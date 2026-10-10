"""Hosted deployment pieces: server config, publisher loop, signing keys, production tokens."""

from __future__ import annotations

import json
import stat
import threading
import uuid

import jwt
import pytest

from empire_platform import automations
from empire_platform.devtoken import generate_keys, key_pair, mint
from tasks_api.app import server_config
from tasks_api.auth import TokenVerifier, Unauthenticated
from tasks_api.publisher import PublishReport, run_loop

PEM = "-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----"


def test_hosted_refuses_dev_issuer_and_missing_pem():
    with pytest.raises(SystemExit):
        server_config({"EMPIRE_DB_MODE": "hosted", "TASKS_API_JWT_PUBLIC_KEY_PEM": PEM})
    with pytest.raises(SystemExit):
        server_config({"EMPIRE_DB_MODE": "hosted", "TASKS_API_JWT_ISSUER": "empire-platform-dev",
                       "TASKS_API_JWT_PUBLIC_KEY_PEM": PEM})
    with pytest.raises(SystemExit):
        server_config({"EMPIRE_DB_MODE": "hosted", "TASKS_API_JWT_ISSUER": "empire-prod"})


def test_pem_env_wins_and_port_precedence(tmp_path):
    file = tmp_path / "public.pem"
    file.write_text("FILE")
    config = server_config({"TASKS_API_JWT_PUBLIC_KEY_PEM": PEM, "TASKS_API_JWT_PUBLIC_KEY": str(file),
                            "TASKS_API_JWT_ISSUER": "empire-prod", "PORT": "8080", "TASKS_API_PORT": "9000",
                            "TASKS_API_HOST": "0.0.0.0"})
    assert (config.public_key_pem, config.issuer, config.port, config.host) == (PEM, "empire-prod", 8080, "0.0.0.0")
    dev = server_config({"TASKS_API_JWT_PUBLIC_KEY": str(file), "TASKS_API_PORT": "9000"})
    assert (dev.public_key_pem, dev.issuer, dev.port, dev.host) == ("FILE", "empire-platform-dev", 9000, "127.0.0.1")
    assert server_config({"TASKS_API_JWT_PUBLIC_KEY": str(file)}).port == 3601
    assert dev.max_lifetime == 3600
    hosted = server_config({"EMPIRE_DB_MODE": "hosted", "TASKS_API_JWT_PUBLIC_KEY_PEM": PEM,
                            "TASKS_API_JWT_ISSUER": "empire-prod"})
    assert hosted.max_lifetime == 30 * 86400


def test_loop_stops_on_iteration_limit_and_signal():
    lines: list[str] = []
    stop = threading.Event()
    assert run_loop(lambda: PublishReport(delivered=2), 0, stop, max_iterations=3, log=lines.append) == 3
    assert [json.loads(line) for line in lines] == [{"delivered": 2, "failed": 0}] * 3
    stop.set()
    assert run_loop(lambda: PublishReport(), 0, stop, log=lines.append) == 0


def test_loop_backs_off_on_errors_and_recovers():
    calls = {"n": 0}
    waits: list[float] = []
    lines: list[str] = []

    class FakeStop(threading.Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            return False

    def step():
        calls["n"] += 1
        if calls["n"] <= 3:
            raise ConnectionError("secret-host.example")
        return PublishReport(delivered=1)

    run_loop(step, 5, FakeStop(), max_iterations=5, log=lines.append)
    assert waits[:3] == [10, 20, 40] and waits[3:] == [5, 5]
    assert "secret-host" not in "".join(lines)
    assert json.loads(lines[0])["error"] == "ConnectionError"


def test_key_generation_permissions_and_no_overwrite(tmp_path):
    directory = tmp_path / "signing"
    public = generate_keys(directory)
    assert public == directory / "public.pem"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "private.pem").stat().st_mode) == 0o600
    before = (directory / "private.pem").read_bytes()
    with pytest.raises(FileExistsError):
        generate_keys(directory)
    assert (directory / "private.pem").read_bytes() == before
    assert automations.main(["keys", "--dir", str(directory)]) == 2


def test_cli_keys_prints_only_public_path(tmp_path, capsys):
    assert automations.main(["keys", "--dir", str(tmp_path / "k")]) == 0
    out = capsys.readouterr().out.strip()
    assert out == str(tmp_path / "k" / "public.pem") and "PRIVATE" not in out


def test_production_tokens(cluster, monkeypatch, tmp_path):
    from conftest import create_workspace
    from empire_platform.permissions import TASKS_OWNER

    with cluster.connect("platform_api") as conn:
        slug = f"prod-{uuid.uuid4().hex[:8]}"
        ws = create_workspace(conn, slug, list(TASKS_OWNER))
        conn.commit()
    monkeypatch.setattr(automations, "default_cluster", lambda: cluster)
    # Production order: a cell's owner permissions come first, then automations can be delegated them.
    from empire_platform import cells

    monkeypatch.setattr(cells, "default_cluster", lambda: cluster)
    assert cells.main(["grant", "--workspace", slug, "--cell", "decisions", "--dry-run"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "decisions"]) == 0
    assert cells.grant(cluster, ws.id, "decisions") == []  # idempotent
    assert cells.main(["grant", "--workspace", slug, "--cell", "habits"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "notifications"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "goals"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "projects"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "roles-skills"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "accounts"]) == 0
    keys, tokens = tmp_path / "keys", tmp_path / "tokens"
    generate_keys(keys)
    assert automations.main(["provision", "--workspace", slug, "--tokens", str(tokens), "--key-dir", str(keys),
                             "--issuer", "empire-prod", "--ttl-days", "30"]) == 0
    assert tokens.joinpath("owner-cli.jwt").is_file()  # provisioned from the registry, with a delegation
    assert automations.main(["owner-token", "--workspace", slug, "--name", "owner-manual", "--tokens", str(tokens),
                             "--key-dir", str(keys), "--issuer", "empire-prod", "--ttl-days", "30"]) == 2
    assert not tokens.joinpath("owner-manual.jwt").exists()
    assert automations.main(["owner-token", "--workspace", slug, "--name", "owner-manual", "--tokens", str(tokens),
                             "--key-dir", str(keys), "--issuer", "empire-prod"]) == 0
    files = sorted(tokens.glob("*.jwt"))
    assert len(files) > 1 and tokens / "owner-manual.jwt" in files
    assert not [p for p in tokens.iterdir() if p.suffix != ".jwt"]  # no leftover temp files
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        claims = jwt.decode(path.read_text(), options={"verify_signature": False})
        short = path.name == "owner-manual.jwt"
        assert claims["iss"] == "empire-prod" and claims["exp"] - claims["iat"] == (3600 if short else 30 * 86400)
    owner = jwt.decode((tokens / "owner-manual.jwt").read_text(), options={"verify_signature": False})
    assert owner["sub"] == str(ws.owner) and "del" not in owner
    bearer = "Bearer " + (tokens / "owner-manual.jwt").read_text()
    pub = (keys / "public.pem").read_text()
    prod = TokenVerifier(pub, "empire-prod", max_lifetime=30 * 86400)
    assert prod.verify(bearer, None).actor == ws.owner
    with pytest.raises(Unauthenticated):
        TokenVerifier(key_pair()[1], "empire-prod", max_lifetime=30 * 86400).verify(bearer, None)
    delegated = "Bearer " + (tokens / "owner-cli.jwt").read_text()
    assert prod.verify(delegated, None).delegation is not None
    with pytest.raises(Unauthenticated):  # the default 1 hour cap still rejects a 30 day delegated token
        TokenVerifier(pub, "empire-prod").verify(delegated, None)


def _token(ttl, delegation=None):
    private, public = key_pair()
    obo = uuid.uuid4() if delegation else None
    token = mint(private, sub=uuid.uuid4(), ws=uuid.uuid4(), delegation=delegation, obo=obo, ttl=ttl, issuer="i")
    return public, "Bearer " + token


def test_tokens_without_delegation_are_capped_at_one_hour_in_every_mode():
    public, long_owner = _token(3601)
    for cap in (3600, 30 * 86400):
        with pytest.raises(Unauthenticated, match="too long"):
            TokenVerifier(public, "i", max_lifetime=cap).verify(long_owner, None)
    public, ok = _token(3600)
    assert TokenVerifier(public, "i", max_lifetime=30 * 86400).verify(ok, None).delegation is None


def test_delegated_tokens_may_live_up_to_the_hosted_max():
    public, token = _token(30 * 86400, uuid.uuid4())
    assert TokenVerifier(public, "i", max_lifetime=30 * 86400).verify(token, None).delegation is not None
    with pytest.raises(Unauthenticated, match="too long"):
        TokenVerifier(public, "i", max_lifetime=7 * 86400).verify(token, None)
    public, over = _token(30 * 86400 + 1, uuid.uuid4())
    with pytest.raises(Unauthenticated, match="too long"):
        TokenVerifier(public, "i", max_lifetime=30 * 86400).verify(over, None)


def test_lifetime_has_a_hard_upper_cap():
    with pytest.raises(ValueError):
        TokenVerifier(PEM, "i", max_lifetime=30 * 86400 + 1)
    with pytest.raises(SystemExit):
        server_config({"EMPIRE_DB_MODE": "hosted", "TASKS_API_JWT_PUBLIC_KEY_PEM": PEM,
                       "TASKS_API_JWT_ISSUER": "empire-prod", "TASKS_API_JWT_MAX_LIFETIME_SECONDS": str(30 * 86400 + 1)})
    ok = server_config({"EMPIRE_DB_MODE": "hosted", "TASKS_API_JWT_PUBLIC_KEY_PEM": PEM,
                        "TASKS_API_JWT_ISSUER": "empire-prod", "TASKS_API_JWT_MAX_LIFETIME_SECONDS": str(30 * 86400)})
    assert ok.max_lifetime == 30 * 86400


def test_hosted_refuses_the_dev_public_key():
    dev_pem = key_pair()[1]
    rewrapped = dev_pem.replace("\n", "\r\n")  # same key, different whitespace
    for pem in (dev_pem, rewrapped):
        with pytest.raises(SystemExit, match="dev public key"):
            server_config({"EMPIRE_DB_MODE": "hosted", "TASKS_API_JWT_PUBLIC_KEY_PEM": pem,
                           "TASKS_API_JWT_ISSUER": "empire-prod"})
    assert server_config({"EMPIRE_DB_MODE": "hosted", "TASKS_API_JWT_PUBLIC_KEY_PEM": PEM,
                          "TASKS_API_JWT_ISSUER": "empire-prod"}).public_key_pem == PEM


def test_owner_cli_is_a_registered_automation():
    registry = automations.load_registry()
    owner = next(a for a in registry["automations"] if a["id"] == "owner-cli")
    assert set(owner["writers"]) == {"infra/cli.ts", "infra/scripts/cli/decision-cli.py",
                                     "infra/scripts/cli/habit-tracker.py", "infra/scripts/cli/routines-cli.py",
                                     "infra/scripts/cli/goals-cli.py", "infra/scripts/cli/projects-cli.py",
                                     "infra/scripts/cli/skills-cli.py", "infra/scripts/learning/learning_evidence.py",
                                     "infra/scripts/sync/sync-github.ts", "infra/scripts/utils/update-skills.ts"}
    assert set(owner["scopes"]) <= {"tasks.task:read", "tasks.task:write", "tasks.status:write",
                                    "decisions.decision:read", "decisions.decision:write", "decisions.decision:approve",
                                    "decisions.decision:delete", "decisions.review:write",
                                    "habits.habit:read", "habits.habit:write", "habits.routine:read",
                                    "habits.routine:write", "notifications.rule:read", "notifications.rule:write",
                                    "notifications.notification:read", "notifications.notification:write",
                                    "goals.goal:read", "goals.goal:write", "goals.goal-task:read",
                                    "goals.goal-task:write", "projects.project:read", "projects.project:write",
                                    "projects.project-repo:read", "projects.project-repo:write",
                                    "roles-skills.skill:read", "roles-skills.skill:write",
                                    "roles-skills.skill-category:read"}
    assert not any("cli" in item["writer"] for item in registry["not_automations"])


def test_default_ttl_is_one_hour(tmp_path):
    token = mint(key_pair()[0], sub=uuid.uuid4(), ws=uuid.uuid4(), ttl=3600)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == 3600


def test_publisher_health_endpoint_follows_the_loop():
    import socket
    import threading
    import urllib.error
    import urllib.request

    from tasks_api.publisher import serve_health

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    stop = threading.Event()
    serve_health(port, stop)
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
        assert response.status == 200
    stop.set()
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
        raise AssertionError("expected 503 after stop")
    except urllib.error.HTTPError as error:
        assert error.code == 503
