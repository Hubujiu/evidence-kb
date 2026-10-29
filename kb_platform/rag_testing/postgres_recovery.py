from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid

from .runner import write_json


class RecoveryRun:
    stages = ("versions", "transaction_rollback", "connection_death", "crash_recovery", "dump_restore", "physical_replication", "promotion")

    def __init__(self, image: str, output: Path):
        self.image, self.output = image, output
        output.mkdir(parents=True, exist_ok=False)
        self.prefix = "kb-recovery-" + uuid.uuid4().hex[:12]
        self.primary, self.standby = self.prefix + "-primary", self.prefix + "-standby"
        self.containers: list[str] = []
        self.commands: list[dict] = []
        self.results = {stage: {"status": "NOT_RUN"} for stage in self.stages}

    def run(self, *args: str, input_text: str | None = None, timeout: int = 120) -> str:
        start = time.monotonic()
        result = subprocess.run(["docker", *args], input=input_text, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        self.commands.append({"args": list(args), "exit_code": result.returncode, "seconds": time.monotonic()-start, "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            raise RuntimeError(f"docker {' '.join(args)}: {result.stderr}")
        return result.stdout.strip()

    def sql(self, query: str, container: str | None = None, database: str = "postgres") -> str:
        return self.run("exec", "-i", container or self.primary, "psql", "-X", "-h", "127.0.0.1", "-U", "postgres", "-d", database, "-v", "ON_ERROR_STOP=1", "-Atq", input_text=query)

    def wait_ready(self, container: str) -> None:
        deadline = time.monotonic()+90
        while time.monotonic() < deadline:
            try:
                if self.sql("SELECT 1", container) == "1":
                    return
            except RuntimeError:
                time.sleep(0.5)
        raise RuntimeError(f"PostgreSQL did not become ready: {container}")

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def rows(self, container: str | None = None, database: str = "postgres") -> str:
        return self.sql("SELECT id FROM items ORDER BY id", container, database)

    def search(self, expected: int, container: str | None = None, database: str = "postgres") -> None:
        self.check(self.sql("SELECT id FROM items ORDER BY body <@> to_bm25query('durable', 'items_bm25') LIMIT 1", container, database) == str(expected), "BM25 durable item was not first")
        self.check(self.sql("SELECT id FROM items ORDER BY embedding <=> '[1,0,0]'::vector LIMIT 1", container, database) == str(expected), "Exact vector durable item was not first")

    def sleeping_transaction(self, item_id: int) -> subprocess.Popen:
        process = subprocess.Popen(["docker", "exec", "-i", self.primary, "psql", "-X", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-Atq"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        process.stdin.write(f"SET application_name='recovery_pending'; BEGIN; INSERT INTO items VALUES ({item_id},'uncommitted phantom','[0,1,0]'); SELECT pg_sleep(300); COMMIT;\n")
        process.stdin.flush()
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            if self.sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='recovery_pending' AND wait_event='PgSleep'") == "1":
                return process
            if process.poll() is not None:
                raise RuntimeError(process.communicate()[1])
            time.sleep(0.1)
        process.terminate()
        raise RuntimeError("Uncommitted test transaction did not reach sleep")

    def execute(self) -> dict:
        stage = "versions"
        try:
            self.run("network", "create", "--internal", self.prefix)
            self.containers.append(self.primary)
            self.run("run", "-d", "--name", self.primary, "--network", self.prefix, "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "PGDATA=/var/lib/postgresql/data/pgdata", "-v", self.prefix+"-primary:/var/lib/postgresql/data", self.image)
            self.wait_ready(self.primary)
            self.sql("CREATE EXTENSION vector; CREATE EXTENSION pg_textsearch;")
            versions = self.sql("SELECT extname||'='||extversion FROM pg_extension WHERE extname IN ('vector','pg_textsearch') ORDER BY extname")
            self.check(versions == "pg_textsearch=1.5.1\nvector=0.8.6", versions)
            self.check(self.sql("SHOW fsync") == "on" and self.sql("SHOW synchronous_commit") == "on", "Durability configuration disabled")
            self.results[stage] = {"status":"PASS", "extensions":versions, "postgres":self.sql("SELECT version()")}
            stage = "transaction_rollback"
            self.sql("CREATE TABLE items(id bigint PRIMARY KEY, body text NOT NULL, embedding vector(3) NOT NULL); CREATE INDEX items_bm25 ON items USING bm25(body) WITH(text_config='simple'); BEGIN; INSERT INTO items VALUES(1,'durable committed document','[1,0,0]'); COMMIT; BEGIN; INSERT INTO items VALUES(2,'rolledback phantom','[0,1,0]'); ROLLBACK;")
            self.check(self.rows() == "1", "Rollback retained a row")
            self.search(1)
            self.results[stage] = {"status":"PASS"}
            stage = "connection_death"
            process = self.sleeping_transaction(3)
            self.sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='recovery_pending'")
            _, error = process.communicate(timeout=15)
            self.check(process.returncode != 0 and self.rows() == "1", "Terminated connection committed")
            self.search(1)
            self.results[stage] = {"status":"PASS", "termination":"server terminates client backend during open transaction", "client_error":error}
            stage = "crash_recovery"
            process = self.sleeping_transaction(4)
            self.run("kill", "--signal=KILL", self.primary)
            process.communicate(timeout=15)
            self.run("start", self.primary)
            self.wait_ready(self.primary)
            self.check(self.rows() == "1", "Crash lost committed data or retained uncommitted data")
            self.search(1)
            self.results[stage] = {"status":"PASS", "fault":"SIGKILL primary with one committed and one open transaction"}
            stage = "dump_restore"
            self.run("exec", self.primary, "pg_dump", "-U", "postgres", "-Fc", "-f", "/tmp/recovery.dump", "postgres")
            self.run("cp", self.primary+":/tmp/recovery.dump", str(self.output/"recovery.dump"))
            self.sql("CREATE DATABASE restored")
            self.run("exec", self.primary, "pg_restore", "-U", "postgres", "--exit-on-error", "-d", "restored", "/tmp/recovery.dump")
            self.check(self.rows(database="restored") == "1", "Dump restore changed rows")
            self.search(1, database="restored")
            self.results[stage] = {"status":"PASS"}
            stage = "physical_replication"
            self.sql("SELECT pg_create_physical_replication_slot('recovery_standby')")
            self.run("exec", self.primary, "sh", "-c", "printf '\nhost replication postgres all trust\n' >> \"$PGDATA/pg_hba.conf\"")
            self.sql("SELECT pg_reload_conf()")
            self.containers.append(self.standby)
            bootstrap = 'mkdir -p "$PGDATA" && chown postgres:postgres "$PGDATA" && chmod 700 "$PGDATA" && gosu postgres pg_basebackup -h '+self.primary+' -U postgres -D "$PGDATA" -R -X stream -c fast -S recovery_standby && exec gosu postgres postgres -c shared_preload_libraries=pg_textsearch -c pg_textsearch.memory_limit=128MB -c hot_standby_feedback=on'
            self.run("run", "-d", "--name", self.standby, "--network", self.prefix, "-e", "PGDATA=/var/lib/postgresql/data/pgdata", "-v", self.prefix+"-standby:/var/lib/postgresql/data", "--entrypoint", "bash", self.image, "-c", bootstrap)
            self.wait_ready(self.standby)
            self.check(self.sql("SELECT pg_is_in_recovery()", self.standby) == "t", "Standby is not recovering")
            self.sql("INSERT INTO items VALUES(5,'replicated document','[0,0,1]')")
            lsn = self.sql("SELECT pg_current_wal_flush_lsn()")
            deadline = time.monotonic()+60
            while self.sql(f"SELECT pg_last_wal_replay_lsn()>='{lsn}'::pg_lsn", self.standby) != "t":
                self.check(time.monotonic()<deadline, "Standby WAL replay timed out")
                time.sleep(0.2)
            self.check(self.rows(self.standby) == "1\n5", "Standby rows differ")
            self.search(1, self.standby)
            self.check(self.sql("SELECT id FROM items ORDER BY body <@> to_bm25query('replicated','items_bm25') LIMIT 1", self.standby) == "5", "Standby did not replay BM25 insert")
            self.results[stage] = {"status":"PASS", "replayed_lsn":lsn}
            stage = "promotion"
            self.run("stop", "-t", "15", self.primary)
            self.check(self.sql("SELECT pg_promote(true,30)", self.standby) == "t", "Promotion failed")
            self.sql("INSERT INTO items VALUES(6,'promoted writable document','[0,1,0]')", self.standby)
            self.check(self.rows(self.standby) == "1\n5\n6", "Promoted standby not writable")
            self.search(1, self.standby)
            self.check(self.sql("SELECT id FROM items ORDER BY body <@> to_bm25query('promoted','items_bm25') LIMIT 1", self.standby) == "6", "Promoted BM25 index not writable")
            self.results[stage] = {"status":"PASS", "scope":"manual promotion after fencing primary; no automatic failover or RPO guarantee"}
        except Exception as error:
            self.results[stage] = {"status":"ERROR", "error":repr(error)}
        finally:
            for container in self.containers:
                try:
                    logs = self.run("logs", container)
                    (self.output/(container+".log")).write_text(logs+"\n"+self.commands[-1]["stderr"], encoding="utf-8")
                    self.run("stop", "-t", "15", container)
                except Exception as error:
                    self.commands.append({"cleanup_error":repr(error), "container":container})
            report = {"kind":"component fault recovery, synthetic data", "status":"PASS" if all(r["status"]=="PASS" for r in self.results.values()) else "FAIL", "image":self.image, "resource_prefix":self.prefix, "resources_preserved":True, "new_remote_calls":0, "stages":self.results}
            write_json(self.output/"commands.json", self.commands)
            write_json(self.output/"report.json", report)
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description='Isolated real PostgreSQL fault/recovery checks; no model or embedding calls.')
    parser.add_argument("--image", default="evidence-kb-postgres:18.6-textsearch1.5.1-vector0.8.6")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = RecoveryRun(args.image, args.output).execute()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
