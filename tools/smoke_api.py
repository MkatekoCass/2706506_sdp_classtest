"""End-to-end smoke test of the REST API on a real repository.

Exercises the *whole* pipeline: local ingestion -> background analysis job ->
every API endpoint, including filtering, author merging, paging, manual
commit lists and CSV export.  Run from the project root::

    python tools/smoke_api.py [path/to/repo.git]

Defaults to ``data/repos/cjson.git`` (a bare clone of cJSON).  The test uses
a scratch data directory (``data/tests/api``) so it never touches real state.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from rat import api as api_mod          # noqa: E402
from rat.registry import RepoRegistry   # noqa: E402

DATA = os.path.join(ROOT, "data", "tests", "api")
DEFAULT_REPO = os.path.join(ROOT, "data", "repos", "cjson.git")

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


def _git(repo_path: str, *args: str) -> str:
    """Run git directly -- the smoke test's independent source of truth."""
    import subprocess
    return subprocess.run(["git", "--git-dir", repo_path, *args],
                          capture_output=True, text=True).stdout


def main() -> int:
    repo_path = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REPO)
    if not os.path.exists(repo_path):
        print(f"repository not found: {repo_path}")
        return 2
    if os.path.exists(DATA):
        shutil.rmtree(DATA)
    os.makedirs(DATA)

    print(f"== ingestion + background job ({repo_path}) ==")
    registry = RepoRegistry(DATA)
    repo = registry.create("cjson", "local", repo_path, analysis_ref="HEAD")
    rid = repo["id"]
    started = time.time()
    api_mod._work(registry, rid, kind="local", archive=None, ref=None)()
    state = registry.get(rid)
    check("job finished", state["status"] == "ready", state.get("error") or "")
    stats = state.get("stats") or {}
    check("stats present", bool(stats.get("commits")), json.dumps(stats)[:200])
    print(f"  indexed in {time.time() - started:.1f}s: "
          f"{stats.get('commits')} commits, {stats.get('files')} files, "
          f"{stats.get('changes')} rows")

    # A fresh registry + app over the same data dir: simulates a server
    # restart, so the test also covers registry/index reloading.
    from app import create_app  # noqa: E402

    app = create_app(DATA)
    client = app.test_client()

    def get(endpoint, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"/api{endpoint}" + (f"?{query}" if query else "")
        response = client.get(url)
        return response.status_code, response

    print("== repository management ==")
    code, resp = get("/health")
    check("health", code == 200 and resp.json["ok"])
    code, resp = get("/repos")
    check("repo list reloads after restart",
          code == 200 and len(resp.json["repos"]) == 1
          and resp.json["repos"][0]["status"] == "ready")

    print("== overview (repository metrics) ==")
    code, resp = get(f"/repos/{rid}/overview")
    check("overview 200", code == 200, resp.data[:200])
    body = resp.json
    m = body["metrics"]
    raw_expected = int(_git(repo_path, "rev-list", "--count", "--no-merges",
                            "HEAD").strip())
    check("commit set = non-merge reachable from HEAD",
          body["commit_set"]["count"] == raw_expected,
          f"{body['commit_set']['count']} != {raw_expected}")
    check("repository metrics = root directory metrics",
          m["added"] == stats["added"] and m["removed"] == stats["removed"])
    check("churn-rate rho = lambda/|H|",
          abs(m["churn_rate"] - round(m["churn"] / m["commits"], 6)) < 1e-9,
          f"{m['churn_rate']} vs {m['churn'] / m['commits']}")
    check("timeline has points", len(body["timeline"]["points"]) > 0)
    check("calendar has cells", len(body["calendar"]["cells"]) > 0)
    check("tree root is the repository",
          body["tree"]["path"] == "" and body["tree"]["value"] == m["churn"])
    check("overview lists top authors with omega share <= 1",
          bool(body["authors"]) and 0 < sum(a["share"] for a in body["authors"]) <= 1,
          f"{len(body['authors'])} authors")
    check("recent commits", len(body["recent"]) == 12)

    print("== filtering: time window ==")
    first_ts = body["commit_set"]["first"]
    last_ts = body["commit_set"]["last"]
    mid = (first_ts + last_ts) // 2
    code, resp = get(f"/repos/{rid}/overview", mode="range", **{"from": mid})
    h_t = resp.json["commit_set"]
    check("H_t is a subset", 0 < h_t["count"] < body["commit_set"]["count"])
    code, resp = get(f"/repos/{rid}/overview", mode="range",
                     **{"from": first_ts, "to": last_ts + 1})
    check("full window == all history",
          resp.json["commit_set"]["count"] == body["commit_set"]["count"])
    code, resp = get(f"/repos/{rid}/overview", mode="range",
                     **{"from": "2019-01-01", "to": "2020-01-01"})
    check("ISO dates accepted", code == 200 and "2019" in resp.json["commit_set"]["label"])

    print("== filtering: authors ==")
    author_id = body["authors"][0]["id"]
    code, resp = get(f"/repos/{rid}/overview", authors=author_id)
    restricted = resp.json["metrics"]
    check("author filter reduces churn",
           restricted["churn"] < m["churn"] and restricted["churn"] > 0)
    check("|H| unchanged by author filter",
          resp.json["commit_set"]["count"] == body["commit_set"]["count"])
    code, resp = get(f"/repos/{rid}/authors")
    top = resp.json["authors"][0]
    check("author table matches overview",
          top["churn"] == body["authors"][0]["churn"]
          and top["commits"] > 0 and top["mods"] == top["commits"])
    check("author omega shares sum to 1",
          abs(sum(a["share"] for a in resp.json["authors"]) - 1) < 1e-3,
          f"{sum(a['share'] for a in resp.json['authors'])}")
    check("author churn sums to repository churn",
          sum(a["churn"] for a in resp.json["authors"]) == m["churn"])

    print("== browse: directories and files ==")
    code, resp = get(f"/repos/{rid}/browse")
    root = resp.json
    check("root children sum to repository churn",
          sum(c["churn"] for c in root["children"]) == m["churn"],
          f"{sum(c['churn'] for c in root['children'])} != {m['churn']}")
    dirs = [c for c in root["children"] if c["type"] == "dir"]
    files = [c for c in root["children"] if c["type"] == "file"]
    check("root has both files and dirs", bool(dirs) and bool(files))
    check("owner attribution present",
          all(c["owner"] for c in root["children"] if c["churn"] > 0))

    target_dir = dirs[0]["path"]
    code, resp = get(f"/repos/{rid}/browse", path=target_dir, kind="dir")
    sub = resp.json
    check(f"browse dir {target_dir!r}", code == 200 and sub["target"]["kind"] == "dir")
    check("dir metric = sum of immediate children",
          sub["object"]["churn"] == sum(c["churn"] for c in sub["children"]))
    check("dir metrics nested inside parent",
          sub["object"]["churn"] <= dirs[0]["churn"])
    check("breadcrumbs", [c["path"] for c in sub["breadcrumbs"]][-1] == target_dir)

    a_file = files[0]["path"]
    code, resp = get(f"/repos/{rid}/browse", path=a_file, kind="file")
    obj = resp.json["object"]
    check(f"browse file {a_file!r}", code == 200 and obj["path"] == a_file)
    check("file deltas consistent",
          obj["growth"] == obj["added"] - obj["removed"]
          and obj["churn"] == obj["added"] + obj["removed"])
    check("file ownership omega sums to 1",
          abs(sum(o["share"] for o in obj["ownership"]) - 1) < 1e-4,
          f"{sum(o['share'] for o in obj['ownership'])}")
    check("file series present", len(obj["series"]) > 0)
    check("rename history is a list", isinstance(obj["renames"], list))

    print("== commits: paging, manual selection, detail ==")
    code, resp = get(f"/repos/{rid}/commits", size=5)
    page = resp.json
    check("commit page", code == 200 and len(page["items"]) == 5
          and page["total"] == body["commit_set"]["count"])
    c0 = page["items"][0]
    check("commits sorted newest first",
          all(page["items"][i]["ct"] >= page["items"][i + 1]["ct"]
              for i in range(len(page["items"]) - 1)))
    picked = [it["hash"] for it in page["items"][:3]]
    code, resp = get(f"/repos/{rid}/commits", mode="list", commits=",".join(picked))
    manual = resp.json
    check("manual commit list resolved",
          manual["commit_set"]["count"] == 3 and manual["commit_set"]["dropped"] == 0)
    check("selected flags set in list mode",
          sum(1 for it in manual["items"] if it.get("selected")) == 3)
    code, resp = get(f"/repos/{rid}/overview", mode="list", commits=",".join(picked))
    check("metrics restricted to manual set",
          resp.json["commit_set"]["count"] == 3
          and resp.json["metrics"]["churn"] <= m["churn"])
    code, resp = get(f"/repos/{rid}/commits", mode="list",
                     commits=picked[0][:10] + ",deadbeef")
    check("abbreviated hash works, unknown hash dropped",
          resp.json["commit_set"]["count"] == 1
          and resp.json["commit_set"]["dropped"] == 1)

    code, resp = get(f"/repos/{rid}/commit/{c0['hash']}")
    detail = resp.json["commit"]
    check("commit detail", code == 200 and detail["hash"] == c0["hash"])
    check("commit detail sums its file rows",
          sum(f["churn"] for f in detail["changes"]) == detail["churn"]
          if detail["changes"] else True)

    print("== author merging ==")
    code, resp = get(f"/repos/{rid}/identities")
    ident = resp.json
    check("identities payload", code == 200 and ident["groups"]
          and not ident["merges"])
    two = ident["raw"][:2]
    groups = [{"label": "SMOKE", "members": [[two[0]["name"], two[0]["email"]],
                                             [two[1]["name"], two[1]["email"]]]}]
    resp = client.post(f"/api/repos/{rid}/merges", json={"groups": groups})
    check("merges accepted", resp.status_code == 200, resp.data[:200])
    code, resp = get(f"/repos/{rid}/authors")
    merged = [a for a in resp.json["authors"] if a["label"] == "SMOKE"]
    check("merged author appears", len(merged) == 1 and merged[0]["merged"],
          f"{len(merged)} rows")
    code, resp = get(f"/repos/{rid}/identities")
    check("merge persisted", resp.json["merges"] == groups)
    client.post(f"/api/repos/{rid}/merges", json={"groups": []})
    code, resp = get(f"/repos/{rid}/authors")
    check("merges cleared", not [a for a in resp.json["authors"]
                                 if a["label"] == "SMOKE"])

    print("== search, refs, export ==")
    code, resp = get(f"/repos/{rid}/search", q="parse")
    found = resp.json
    check("search finds files", code == 200 and bool(found["files"]))
    check("search finds commits", bool(found["commits"]))
    code, resp = get(f"/repos/{rid}/refs")
    refs = resp.json
    check("refs listed", code == 200 and any(r["name"] == "HEAD"
                                             for r in refs["refs"]))
    for kind, needle in (("files", "path"), ("dirs", "path"),
                         ("authors", "author"), ("commits", "hash")):
        code, resp = get(f"/repos/{rid}/export.csv", kind=kind)
        text = resp.data.decode()
        check(f"export {kind}", code == 200 and needle in text.splitlines()[0]
              and len(text.splitlines()) > 1,
              text[:120])
    code, resp = get(f"/repos/{rid}/export.csv", kind="nonsense")
    check("bad export kind rejected", code == 400)

    print("== error handling ==")
    code, resp = get("/repos/nope/overview")
    check("unknown repo -> 404", code == 404 and "error" in resp.json)
    code, resp = get(f"/repos/{rid}/browse", path="does/not/exist")
    check("unknown path -> 404", code == 404)
    code, resp = get(f"/repos/{rid}/overview", mode="weird")
    check("bad mode -> 400", code == 400)
    code, resp = get(f"/repos/{rid}/overview", **{"from": "yesterday"})
    check("bad timestamp -> 400", code == 400)

    print()
    print(f"{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
