"""Local PostgreSQL for development and tests, plus the migration runner.

The cluster lives under ``.runtime/pgdata`` and listens only on a Unix socket,
so nothing is exposed on the network and no credentials are needed. Hosted
databases (Neon) are out of scope here and need separate owner approval.

    empire-db start | stop | reset | status | migrate | psql [--role ROLE]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "db" / "bootstrap" / "roles.sql"
MIGRATION_DIRS = [REPO_ROOT / "services" / "platform" / "migrations"]
DATABASE = "empire"
SUPERUSER = "postgres"
PORT = 5432  # only used to name the Unix socket file; TCP is disabled


def pg_bin_dir() -> Path:
    """PostgreSQL binaries: $EMPIRE_PG_BIN, the pgserver wheel, or PATH."""
    override = os.environ.get("EMPIRE_PG_BIN")
    if override:
        return Path(override)
    try:
        import pgserver  # type: ignore[import-untyped]

        bundled = Path(pgserver.__file__).parent / "pginstall" / "bin"
        if (bundled / "pg_ctl").exists():
            return bundled
    except ImportError:
        pass
    found = shutil.which("pg_ctl")
    if found:
        return Path(found).parent
    raise SystemExit("PostgreSQL binaries not found; install the dev extra or set EMPIRE_PG_BIN")


@dataclass
class Cluster:
    data_dir: Path
    socket_dir: Path

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(pg_bin_dir() / args[0]), *args[1:]], check=True, capture_output=True, text=True
        )

    def init(self) -> None:
        self.data_dir.parent.mkdir(parents=True, exist_ok=True)
        self._run("initdb", "-D", str(self.data_dir), "-U", SUPERUSER, "--auth=trust",
                  "--encoding=UTF8", "--locale=C")

    def start(self) -> None:
        self.socket_dir.mkdir(parents=True, exist_ok=True)
        options = f"-c listen_addresses='' -k {self.socket_dir} -p {PORT}"
        self._run("pg_ctl", "-D", str(self.data_dir), "-o", options, "-w",
                  "-l", str(self.data_dir / "server.log"), "start")

    def stop(self) -> None:
        if (self.data_dir / "postmaster.pid").exists():
            self._run("pg_ctl", "-D", str(self.data_dir), "-m", "fast", "-w", "stop")

    def running(self) -> bool:
        return (self.data_dir / "postmaster.pid").exists()

    def conninfo(self, role: str, dbname: str = DATABASE) -> str:
        return f"host={self.socket_dir} port={PORT} user={role} dbname={dbname}"

    def connect(self, role: str, dbname: str = DATABASE, **kwargs) -> psycopg.Connection:
        return psycopg.connect(self.conninfo(role, dbname), **kwargs)


def default_cluster() -> Cluster:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
    return Cluster(REPO_ROOT / ".runtime" / "pgdata", runtime / "empire-platform")


def bootstrap(cluster: Cluster) -> None:
    """Create roles and the database; safe to run more than once."""
    with cluster.connect(SUPERUSER, "postgres", autocommit=True) as conn:
        conn.execute(BOOTSTRAP.read_text(encoding="utf-8"))
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,)).fetchone()
        if not exists:
            conn.execute(f"CREATE DATABASE {DATABASE} OWNER empire_migrator")
    with cluster.connect(SUPERUSER, DATABASE, autocommit=True) as conn:
        conn.execute(f"REVOKE ALL ON DATABASE {DATABASE} FROM PUBLIC")
        conn.execute(f"GRANT CONNECT ON DATABASE {DATABASE} TO platform_api")
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")


def migration_files() -> list[Path]:
    files = [path for directory in MIGRATION_DIRS for path in sorted(directory.glob("*.sql"))]
    names = [path.name for path in files]
    if len(names) != len(set(names)):
        raise SystemExit("duplicate migration file names")
    return files


def migrate(cluster: Cluster) -> list[str]:
    """Apply pending migrations as empire_migrator; refuse edited ones."""
    applied: list[str] = []
    with cluster.connect("empire_migrator") as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
            " name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        conn.commit()
        done = dict(conn.execute("SELECT name, sha256 FROM public.schema_migrations").fetchall())
        for path in migration_files():
            sql = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(sql.encode()).hexdigest()
            if path.name in done:
                if done[path.name] != digest:
                    raise SystemExit(f"applied migration was edited: {path.name}")
                continue
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO public.schema_migrations (name, sha256) VALUES (%s, %s)",
                    (path.name, digest),
                )
            applied.append(path.name)
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="empire-db", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["start", "stop", "reset", "status", "migrate", "psql"])
    parser.add_argument("--role", default="platform_api")
    args = parser.parse_args(argv)
    cluster = default_cluster()

    if args.command == "reset":
        cluster.stop()
        shutil.rmtree(cluster.data_dir, ignore_errors=True)
    if args.command in {"start", "reset"}:
        if not (cluster.data_dir / "PG_VERSION").exists():
            cluster.init()
        if not cluster.running():
            cluster.start()
        bootstrap(cluster)
        for name in migrate(cluster):
            print(f"applied {name}")
        print(f"ready: {cluster.conninfo('platform_api')}")
    elif args.command == "stop":
        cluster.stop()
        print("stopped")
    elif args.command == "status":
        print(f"{'running' if cluster.running() else 'stopped'}: {cluster.data_dir}")
    elif args.command == "migrate":
        applied = migrate(cluster)
        print("\n".join(f"applied {name}" for name in applied) or "up to date")
    elif args.command == "psql":
        os.execv(str(pg_bin_dir() / "psql"), ["psql", cluster.conninfo(args.role)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
