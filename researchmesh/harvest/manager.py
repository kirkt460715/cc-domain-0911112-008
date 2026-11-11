"""采集运行管理：分页暂存、时间边界、重采与落库。

设计要点（对应科研管理部的盘点风险）：

* 每个 run 在启动时固定 from/until 时间边界与 resumptionToken 起点；
  token 过期后从同一边界重采，绝不悄悄更换截止点。
* 每页解析成功即独立事务落库，中途失败只把 run 标记为 partial，
  已采集页保留，缺席项不会被撤回。
* 只有 mode=full 且完整遍历（且期间未发生 generation 漂移）才允许
  完整快照；其余情况一律走变更键（upsert/withdraw），只动明确项。
* generation 漂移时 run 原样保留并要求显式 rebase；rebase 后也只按
  变更键处理，避免覆盖并发的人工提交。
"""
import hashlib
import uuid

from ..contracts import (
    MeshError,
    identifier,
    integer,
    bounded_text,
    canonical,
    decode,
    digest,
    fail,
    source_contract,
)
from .oai import OAIError, build_url, parse_page, urllib_transport, day_precision

MAX_PAGES = 1000
MAX_RESTARTS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS harvest_runs(id TEXT PRIMARY KEY,source TEXT NOT NULL,
 endpoint TEXT NOT NULL,mode TEXT NOT NULL,from_day TEXT,until_day TEXT,
 metadata_prefix TEXT NOT NULL,expected_generation INTEGER NOT NULL,
 max_restarts INTEGER NOT NULL,
 restarts INTEGER NOT NULL DEFAULT 0,token TEXT,state TEXT NOT NULL,
 had_failure INTEGER NOT NULL DEFAULT 0,
 params_digest TEXT NOT NULL,error TEXT,watermark TEXT,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
CREATE TABLE IF NOT EXISTS harvest_locks(run TEXT PRIMARY KEY,holder TEXT NOT NULL,
 acquired TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
CREATE TABLE IF NOT EXISTS harvest_pages(run TEXT NOT NULL,seq INTEGER NOT NULL,
 token TEXT,payload TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(run,seq));
CREATE TABLE IF NOT EXISTS harvest_page_skips(run TEXT NOT NULL,seq INTEGER NOT NULL,
 digest TEXT NOT NULL,PRIMARY KEY(run,seq));
CREATE TABLE IF NOT EXISTS harvest_identifiers(source TEXT NOT NULL,local_id TEXT NOT NULL,
 external_id TEXT NOT NULL,PRIMARY KEY(source,local_id),UNIQUE(source,external_id));
CREATE TABLE IF NOT EXISTS harvest_watermarks(source TEXT PRIMARY KEY,day TEXT NOT NULL,run TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS harvest_applies(run TEXT PRIMARY KEY,result TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS harvest_rebases(run TEXT NOT NULL,seq INTEGER NOT NULL,
 from_generation INTEGER NOT NULL,to_generation INTEGER NOT NULL,reason TEXT NOT NULL,
 PRIMARY KEY(run,seq));
CREATE TABLE IF NOT EXISTS harvest_dead_tokens(run TEXT NOT NULL,token TEXT NOT NULL,
 PRIMARY KEY(run,token));
"""


def local_identifier_for(external_id):
    """把稳定外部 identifier（可能含冒号）映射为本地记录 id。

    哈希是确定性的：同一外部 identifier 永远映射到同一本地 id，
    且本地 id 只含标识符安全字符。
    """
    value = bounded_text(external_id, 2000)
    return "ext" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class HarvestManager:
    def __init__(self, store, sources, changes, transport=None):
        self.store = store
        self.sources = sources
        self.changes = changes
        self.transport = transport or urllib_transport
        self.store.connection.executescript(SCHEMA)

    # ---------- 运行台账 ----------

    def _row(self, run):
        row = self.store.one("SELECT * FROM harvest_runs WHERE id=?", (run,))
        if row is None:
            fail("missing", "harvest run not found", run=run)
        return row

    def get(self, run):
        row = self._row(run)
        result = {key: row[key] for key in row.keys()}
        if result["error"]:
            result["error"] = decode(result["error"])
        result["applied"] = self.store.one(
            "SELECT 1 FROM harvest_applies WHERE run=?", (run,)
        ) is not None
        return result

    def list_runs(self, source=None):
        if source is not None:
            self.sources.get(source)
            rows = self.store.rows(
                "SELECT * FROM harvest_runs WHERE source=? ORDER BY created_at,id",
                (source,),
            )
        else:
            rows = self.store.rows(
                "SELECT * FROM harvest_runs ORDER BY created_at,id"
            )
        return [
            {
                "run": row["id"],
                "source": row["source"],
                "mode": row["mode"],
                "state": row["state"],
                "from_day": row["from_day"],
                "until_day": row["until_day"],
                "expected_generation": row["expected_generation"],
                "restarts": row["restarts"],
                "had_failure": bool(row["had_failure"]),
            }
            for row in rows
        ]

    def watermark(self, source):
        self.sources.get(source)
        row = self.store.one(
            "SELECT * FROM harvest_watermarks WHERE source=?", (source,)
        )
        if row is None:
            return {"source": source, "day": None, "run": None}
        return {"source": source, "day": row["day"], "run": row["run"]}

    def start(
        self,
        run,
        source,
        endpoint,
        mode,
        from_day=None,
        until_day=None,
        metadata_prefix="oai_dc",
        max_restarts=MAX_RESTARTS,
    ):
        identifier(run)
        self.sources.get(source)
        if mode not in {"full", "incremental"}:
            fail("input", "harvest mode must be full or incremental")
        bounded_text(endpoint, 2000)
        bounded_text(metadata_prefix, 100)
        integer(max_restarts, 0, 20)
        if from_day is not None:
            from_day = day_precision(from_day)
        if until_day is not None:
            until_day = day_precision(until_day)
        if from_day and until_day and from_day > until_day:
            fail("input", "harvest from boundary must not be after until boundary")
        params = {
            "source": source,
            "endpoint": endpoint,
            "mode": mode,
            "from_day": from_day,
            "until_day": until_day,
            "metadata_prefix": metadata_prefix,
            "max_restarts": max_restarts,
        }
        params_digest = digest(params)
        with self.store.atomic() as con:
            existing = self.store.one(
                "SELECT * FROM harvest_runs WHERE id=?", (run,)
            )
            if existing:
                if existing["params_digest"] != params_digest:
                    fail("conflict", "harvest run identity already exists with other boundaries")
                return self.get(run)
            generation = self.sources.get(source)["generation"]
            if mode == "incremental" and from_day is None:
                mark = self.store.one(
                    "SELECT day FROM harvest_watermarks WHERE source=?", (source,)
                )
                if mark is None:
                    fail(
                        "input",
                        "incremental harvest needs an explicit from boundary or a prior watermark",
                    )
                from_day = mark["day"]
                params["from_day"] = from_day
                params_digest = digest(params)
            con.execute(
                "INSERT INTO harvest_runs(id,source,endpoint,mode,from_day,until_day,"
                "metadata_prefix,expected_generation,max_restarts,state,params_digest)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run, source, endpoint, mode, from_day, until_day,
                    metadata_prefix, generation, max_restarts, "collecting",
                    params_digest,
                ),
            )
            self.store.audit("harvest_started", run, params | {"expected_generation": generation})
        return self.get(run)

    # ---------- 分页采集 ----------

    def _record_failure(self, run, exc):
        payload = exc.record() if isinstance(exc, OAIError) else {
            "code": "harvest_failed", "message": str(exc)
        }
        with self.store.atomic() as con:
            row = self._row(run)
            if row["state"] == "collecting":
                con.execute(
                    "UPDATE harvest_runs SET state='partial',had_failure=1,error=? WHERE id=?",
                    (canonical(payload), run),
                )
                self.store.audit("harvest_interrupted", run, payload)

    def _dead_tokens(self, run):
        return {
            row["token"]
            for row in self.store.rows(
                "SELECT token FROM harvest_dead_tokens WHERE run=?", (run,)
            )
        }

    def _acquire_lock(self, run):
        holder = uuid.uuid4().hex
        try:
            with self.store.atomic() as con:
                con.execute(
                    "INSERT INTO harvest_locks(run,holder) VALUES(?,?)",
                    (run, holder),
                )
        except Exception as exc:
            fail(
                "busy",
                "harvest run is already being collected by another worker",
                run=run,
                reason=str(exc),
            )
        return holder

    def _release_lock(self, run, holder):
        with self.store.atomic() as con:
            con.execute(
                "DELETE FROM harvest_locks WHERE run=? AND holder=?", (run, holder)
            )

    def collect(self, run):
        """按 token 拉取并暂存全部页面，返回 run 摘要。

        每页独立事务持久化；resumptionToken 过期（badResumptionToken）
        时从本轮固定的 from/until 边界重采，重复页按摘要去重。同一个
        token 只允许重采一次：若边界重采后服务器仍返回同一个死 token，
        说明问题无法靠重采消除，按中途失败处理，不再循环。
        采集期间持有行级锁，并发只有一个工作者能写入。
        """
        row = self._row(run)
        if row["state"] in {"collected", "applied"}:
            return self.get(run)
        holder = self._acquire_lock(run)
        try:
            return self._collect_locked(run)
        finally:
            self._release_lock(run, holder)

    def _collect_locked(self, run):
        row = self._row(run)
        params = {
            "from_day": row["from_day"],
            "until_day": row["until_day"],
            "metadata_prefix": row["metadata_prefix"],
        }
        max_restarts = self._max_restarts(row)
        token = row["token"]
        tokens_seen = set()
        if token:
            tokens_seen.add(token)
        duplicate_skipped = False
        try:
            while True:
                url = build_url(row["endpoint"], token, **params)
                content, http_status = self._fetch(url)
                page = parse_page(content)
                # 采集期就按现有字段契约清洗，坏页整体拒绝；页面在独立
                # 事务内写入，回滚不影响此前已暂存的页面。
                for entry in page["entries"]:
                    if not entry["deleted"]:
                        source_contract(entry["data"])
                with self.store.atomic() as con:
                    seq = self.store.one(
                        "SELECT COALESCE(MAX(seq),0)+1 AS next FROM harvest_pages WHERE run=?",
                        (run,),
                    )["next"]
                    page_digest = digest(page["entries"])
                    duplicate = self.store.one(
                        "SELECT 1 FROM harvest_pages WHERE run=? AND digest=?",
                        (run, page_digest),
                    )
                    if duplicate is None:
                        if seq > MAX_PAGES:
                            fail("capacity", "harvest exceeds the bounded page count")
                        con.execute(
                            "INSERT INTO harvest_pages VALUES(?,?,?,?,?)",
                            (run, seq, page["token"], canonical(page["entries"]), page_digest),
                        )
                    else:
                        duplicate_skipped = True
                        con.execute(
                            "INSERT OR IGNORE INTO harvest_page_skips VALUES(?,?,?)",
                            (run, seq, page_digest),
                        )
                    con.execute(
                        "UPDATE harvest_runs SET token=? WHERE id=?",
                        (page["token"], run),
                    )
                token = page["token"]
                if page["complete"]:
                    with self.store.atomic() as con:
                        con.execute(
                            "UPDATE harvest_runs SET state='collected',token=NULL,error=NULL WHERE id=?",
                            (run,),
                        )
                        self.store.audit(
                            "harvest_completed",
                            run,
                            {
                                "pages": self._page_count(run),
                                "duplicate_page_skipped": duplicate_skipped,
                            },
                        )
                    return self.get(run)
                if token in tokens_seen:
                    fail("capacity", "OAI resumptionToken cycle detected", token=token)
                tokens_seen.add(token)
                if self._page_count(run) >= MAX_PAGES:
                    fail("capacity", "harvest exceeds the bounded page count")
        except OAIError as exc:
            current_token = self._row(run)["token"]
            already_tried = current_token in self._dead_tokens(run)
            can_restart = (
                exc.code == "badResumptionToken"
                and current_token is not None
                and not already_tried
                and self._row(run)["restarts"] < max_restarts
            )
            if can_restart:
                with self.store.atomic() as con:
                    restarts = self._row(run)["restarts"] + 1
                    con.execute(
                        "INSERT OR IGNORE INTO harvest_dead_tokens VALUES(?,?)",
                        (run, current_token),
                    )
                    con.execute(
                        "UPDATE harvest_runs SET restarts=?,token=NULL WHERE id=?",
                        (restarts, run),
                    )
                    self.store.audit(
                        "harvest_restarted",
                        run,
                        {
                            "reason": exc.record(),
                            "restart": restarts,
                            "dead_token": current_token,
                            "fixed_boundary": True,
                        },
                    )
                return self._collect_locked(run)
            if already_tried:
                exc = OAIError(
                    "resumptionToken remained invalid after a fixed-boundary restart",
                    code="badResumptionToken",
                )
            self._record_failure(run, exc)
            fail("harvest_failed", "OAI harvest stopped before full traversal",
                 run=run, error=exc.record())
        except MeshError as exc:
            # 容量/循环/契约等本地错误同样让 run 进入 partial，保留已采页。
            self._record_failure(run, OAIError(str(exc), code=exc.code))
            fail(
                "harvest_failed",
                "OAI harvest stopped before full traversal",
                run=run,
                error=exc.record(),
            )
        except Exception as exc:
            self._record_failure(run, exc)
            fail("harvest_failed", "OAI harvest stopped before full traversal",
                 run=run, error={"code": "transport", "message": str(exc)})

    def _fetch(self, url):
        return self.transport(url)

    def _max_restarts(self, row):
        return row["max_restarts"]

    def _page_count(self, run):
        return self.store.one(
            "SELECT COUNT(*) AS n FROM harvest_pages WHERE run=?", (run,)
        )["n"]

    # ---------- 对账与解释 ----------

    def _occurrences(self, run):
        """按页顺序展开全部条目，返回 external_id -> 出现列表。"""
        pages = self.store.rows(
            "SELECT seq,payload,digest FROM harvest_pages WHERE run=? ORDER BY seq",
            (run,),
        )
        occurrences = {}
        page_digests = []
        for page in pages:
            page_digests.append(page["digest"])
            for entry in decode(page["payload"]):
                occurrences.setdefault(entry["identifier"], []).append(
                    {**entry, "page_seq": page["seq"]}
                )
        return pages, page_digests, occurrences

    def explain(self, run):
        row = self._row(run)
        pages, page_digests, occurrences = self._occurrences(run)
        repeated_pages = sorted(
            {digest for digest in page_digests if page_digests.count(digest) > 1}
        )
        skipped = self.store.rows(
            "SELECT seq,digest FROM harvest_page_skips WHERE run=? ORDER BY seq", (run,)
        )
        identifiers = []
        for external_id in sorted(occurrences):
            versions = occurrences[external_id]
            chosen = versions[-1]
            identifiers.append(
                {
                    "external_id": external_id,
                    "local_id": local_identifier_for(external_id),
                    "occurrences": len(versions),
                    "versions": [
                        {
                            "page_seq": item["page_seq"],
                            "datestamp": item["datestamp"],
                            "deleted": item["deleted"],
                            "digest": digest(item.get("data")) if "data" in item else None,
                        }
                        for item in versions
                    ],
                    "chosen": {
                        "datestamp": chosen["datestamp"],
                        "deleted": chosen["deleted"],
                        "page_seq": chosen["page_seq"],
                    },
                }
            )
        return {
            "run": run,
            "source": row["source"],
            "mode": row["mode"],
            "state": row["state"],
            "from_day": row["from_day"],
            "until_day": row["until_day"],
            "expected_generation": row["expected_generation"],
            "restarts": row["restarts"],
            "pages": len(pages),
            "duplicate_pages": len(repeated_pages) + len(skipped),
            "skipped_page_seqs": [item["seq"] for item in skipped],
            "multi_version_identifiers": [
                item["external_id"] for item in identifiers if item["occurrences"] > 1
            ],
            "identifiers": identifiers,
            "error": decode(row["error"]) if row["error"] else None,
        }

    def _reconcile(self, run):
        """同一 identifier 多版本时取最后一页版本，返回去重后的决策。"""
        row = self._row(run)
        _, _, occurrences = self._occurrences(run)
        upserts, deletes, multi = [], [], []
        newest_day = None
        for external_id in sorted(occurrences):
            versions = occurrences[external_id]
            chosen = versions[-1]
            newest_day = chosen["datestamp"] if newest_day is None else max(newest_day, chosen["datestamp"])
            if len(versions) > 1:
                multi.append(external_id)
            local_id = local_identifier_for(external_id)
            if chosen["deleted"]:
                deletes.append({"external_id": external_id, "local_id": local_id,
                                "datestamp": chosen["datestamp"]})
            else:
                upserts.append({"external_id": external_id, "local_id": local_id,
                                "datestamp": chosen["datestamp"], "data": chosen["data"]})
        return row, upserts, deletes, multi, newest_day

    # ---------- 落库 ----------

    def _guard_generation(self, row):
        current = self.sources.get(row["source"])["generation"]
        if current != row["expected_generation"]:
            fail(
                "generation",
                "source generation changed after collection; rebase the harvest run before applying",
                run=row["id"],
                expected_generation=row["expected_generation"],
                current_generation=current,
                hint="collected pages are preserved; call harvest.rebase then re-apply",
            )
        return current

    def _bind_mapping(self, con, source, external_id, local_id):
        existing = self.store.one(
            "SELECT external_id FROM harvest_identifiers WHERE source=? AND local_id=?",
            (source, local_id),
        )
        if existing and existing["external_id"] != external_id:
            fail("integrity", "external identifier mapping collision", local_id=local_id)
        con.execute(
            "INSERT OR IGNORE INTO harvest_identifiers VALUES(?,?,?)",
            (source, local_id, external_id),
        )

    def apply(self, run):
        row = self._row(run)
        previous = self.store.one(
            "SELECT * FROM harvest_applies WHERE run=?", (run,)
        )
        if previous:
            return {**decode(previous["result"]), "reused": True}
        if row["state"] == "collecting":
            fail("state", "harvest run is still collecting; call harvest.collect first")
        current_generation = self._guard_generation(row)
        reconciled, upserts, deletes, multi, newest_day = self._reconcile(run)
        source = row["source"]
        rebased = self.store.one(
            "SELECT COUNT(*) AS n FROM harvest_rebases WHERE run=?", (run,)
        )["n"]
        # 完整来源快照只在“全量 + 完整遍历 + 从未中途失败 + 无 generation
        # 漂移”时成立；任何中断/重采/rebase 后都只按明确变更键处理。
        full_snapshot = (
            row["mode"] == "full"
            and row["state"] == "collected"
            and not row["had_failure"]
            and rebased == 0
        )
        with self.store.atomic() as con:
            for item in upserts:
                self._bind_mapping(con, source, item["external_id"], item["local_id"])
            if full_snapshot:
                records = [
                    {"id": item["local_id"], "data": source_contract(item["data"])}
                    for item in upserts
                ]
                result = self.sources.snapshot(
                    run, source, current_generation + 1, records, current_generation, True
                )
                strategy = "complete_snapshot"
            else:
                # 增量、未走完的全量、或 rebase 后的运行：只改明确变更键，
                # 缺席项一律保留，绝不撤回。
                active_rows = self.store.rows(
                    "SELECT local_id,revision,active FROM records WHERE source=?",
                    (source,),
                )
                revisions = {r["local_id"]: r["revision"] for r in active_rows}
                active_ids = {r["local_id"] for r in active_rows if r["active"]}
                operations, ignored_deletes = [], []
                for item in upserts:
                    operations.append({
                        "id": item["local_id"],
                        "operation": "upsert",
                        "expected_revision": revisions.get(item["local_id"], 0),
                        "data": source_contract(item["data"]),
                    })
                for item in deletes:
                    if item["local_id"] not in active_ids:
                        ignored_deletes.append(item["external_id"])
                        continue
                    operations.append({
                        "id": item["local_id"],
                        "operation": "withdraw",
                        "expected_revision": revisions[item["local_id"]],
                    })
                if operations:
                    preview = self.changes.inspect(source, operations, current_generation)
                    result = self.changes.apply(
                        run, source, operations, current_generation, preview["digest"]
                    )
                else:
                    result = {
                        "batch": run, "source": source,
                        "generation": current_generation, "changed": [],
                        "unchanged": [], "withdrawn": [], "reactivated": [],
                        "data_revision": self.store.revision("data"),
                        "reused": False,
                    }
                strategy = "explicit_changes"
            watermark = None
            if row["mode"] == "incremental" and newest_day is not None:
                prior = self.store.one(
                    "SELECT day FROM harvest_watermarks WHERE source=?", (source,)
                )
                watermark = newest_day if prior is None else max(prior["day"], newest_day)
                con.execute(
                    "INSERT INTO harvest_watermarks VALUES(?,?,?) "
                    "ON CONFLICT(source) DO UPDATE SET day=excluded.day,run=excluded.run",
                    (source, watermark, run),
                )
            con.execute(
                "UPDATE harvest_runs SET state='applied',watermark=? WHERE id=?",
                (watermark, run),
            )
            envelope = {
                "run": run,
                "source": source,
                "mode": row["mode"],
                "strategy": strategy,
                "complete_traversal": row["state"] == "collected",
                "pages": self._page_count(run),
                "restarts": row["restarts"],
                "multi_version_identifiers": multi,
                "upserts": len(upserts),
                "deletes": len(deletes),
                "ignored_unmapped_deletes": [
                    item["external_id"] for item in deletes
                    if item["local_id"] not in active_ids
                ] if not full_snapshot else [],
                "watermark": watermark,
                "snapshot": result,
            }
            con.execute(
                "INSERT INTO harvest_applies VALUES(?,?,?)",
                (run, canonical(envelope), digest(envelope)),
            )
            self.store.audit("harvest_applied", run, envelope)
        return {**envelope, "reused": False}

    def rebase(self, run, reason):
        """generation 漂移后的显式确认：保留采集页，只重定基线。"""
        row = self._row(run)
        reason = bounded_text(reason)
        if self.store.one("SELECT 1 FROM harvest_applies WHERE run=?", (run,)):
            fail("state", "applied harvest runs cannot be rebased")
        current = self.sources.get(row["source"])["generation"]
        with self.store.atomic() as con:
            if current == row["expected_generation"]:
                fail("conflict", "harvest run generation is already current")
            seq = self.store.one(
                "SELECT COALESCE(MAX(seq),0)+1 AS next FROM harvest_rebases WHERE run=?",
                (run,),
            )["next"]
            con.execute(
                "INSERT INTO harvest_rebases(run,seq,from_generation,to_generation,reason)"
                " VALUES(?,?,?,?,?)",
                (run, seq, row["expected_generation"], current, reason),
            )
            con.execute(
                "UPDATE harvest_runs SET expected_generation=? WHERE id=?",
                (current, run),
            )
            self.store.audit(
                "harvest_rebased",
                run,
                {
                    "from_generation": row["expected_generation"],
                    "to_generation": current,
                    "reason": reason,
                    "pages_preserved": self._page_count(run),
                },
            )
        return self.get(run)
