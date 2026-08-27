#!/usr/bin/env python3
"""稼働中の担当エージェント・着手中のタスク・トークン消費を、ブラウザで見せる。

何のためか
----------
組織を動かしている最中、PO（プロジェクトオーナー＝人間の所有者）から見える
のはメインセッションの画面だけで、「今どのエージェントが何をしているか」は
分からない。このスクリプトは、その状態を1枚の画面に出すことだけを行う。

**読むだけで、組織の動作には一切干渉しない。** このモニタが落ちても、
起動しなくても、組織はそのまま動く。

どこからデータを取るか
----------------------
新しく記録を取る仕組みは作らない。Claude Code が既に書き出している会話記録
（1行1イベントの JSONL 形式のファイル）を読む。置き場と構造は、トークン
集計スクリプト（`org-tokens.py`）の説明に書いたものと同じ。

  稼働中かどうか   担当エージェント1体につき `agent-XXXX.meta.json` という
                   小さなファイルが書かれる。その中の toolUseId（起動を
                   識別する番号）が、メインセッションの会話記録に「実行結果」
                   の行として現れていなければ、まだ動いている。

  担当と作業内容   同じ meta.json の agentType（どのエージェント定義で
                   起動したか）と description（何をさせているか）。

  タスクID         担当エージェントへの指示文から `T-007` の形を拾う
                   （`org-tokens.py` の関数をそのまま使う）。

  トークン         会話記録の usage 欄。**トークン消費の台帳（CSV）は読まない。**
                   台帳はフックのタイミングでしか更新されず、リアルタイムには
                   遅れるため。

  タスクの状態     タスク台帳の索引 `docs/task-list-*.csv`
                   （`org-check.py` の読み取りをそのまま使う）。

会話記録は数メガバイトになる。毎回全部読むと2秒ごとの更新に耐えないので、
**前回どこまで読んだかを覚えて、増えた分だけ読み足す。**

将来の拡張
----------
画面が読んでいるのは `/api/state` という取得口が返す JSON だけである。
表示を作り込む（ダッシュボード化する）場合も、状態を組み立てる部分
（`Watcher` クラス）はそのまま使い、画面側だけを差し替えられる。

Python 3.8 以降。標準ライブラリのみ。追加インストールは要らない。

使い方:
    python3 org-monitor.py --root path/to/repo         起動してブラウザを開く
    python3 org-monitor.py --root path/to/repo --hook  フックから呼ぶとき
    python3 org-monitor.py --root path/to/repo --once  状態を1回 JSON で出す
    python3 org-monitor.py --root ... --no-open        ブラウザを開かない
    python3 org-monitor.py --root ... --port 7391      待ち受けるポート番号

`python3` という名前のコマンドが無い環境（Windows の標準的な導入ではこれが
普通）では `python` に読み替える。

--hook を付けたときの振る舞い:
    1. タスク台帳が無ければ、何もせずに終わる（組織を動かしていない普通の
       セッションでブラウザが開くのを防ぐ）
    2. 同じリポジトリのモニタが既に動いていれば、新しく立てずにそのURLを開く
    3. 立てる場合は、自分を切り離した別プロセスとして起動し、すぐ戻る
       （フックは呼んだコマンドの終了を待つので、待たせない）

終了コード:
    0  正常
    1  対象のセッションが見つからない（--once のとき）
    2  ポートを確保できない

--hook を付けると、何が起きても 0 で終わる。Claude Code のフックは、終了
コードが 0 のときだけ標準出力をセッションの文脈へ入れる仕様のため。
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

# 待ち受けるポート番号の既定値と、埋まっていたときに探す範囲。
# 同じ機械で複数の開発対象リポジトリを開くことがあるので、1つに固定しない。
BASE_PORT = 7391
PORT_SPAN = 10

# 既に動いているモニタを探すとき、1つのポートを何秒待つか。
PROBE_TIMEOUT = 0.4

# 画面から通信が来なくなってから、何秒でモニタ自身を終わらせるか。
# ブラウザのタブを閉じ忘れても、プロセスが残り続けないようにするため。
IDLE_TIMEOUT = 600

# 画面が状態を取りに来る間隔（秒）。画面側の JavaScript と合わせる。
REFRESH = 2

# 状態を組み立て直す最短間隔（秒）。画面を複数開かれても、
# 会話記録の読み直しがその回数だけ走らないようにする。
MIN_REBUILD = 0.5

# 対象セッションを探し直す間隔（秒）。この探索は ~/.claude/projects/ の下の
# 会話記録を総なめするため、2秒ごとにやると重い。新しいセッションが始まった
# ことに気づくのが最大この秒数だけ遅れるが、実用上は問題にならない。
SESSION_RESCAN = 30

# エージェント定義の名前を、画面に出す短い呼び名へ直す。
# ここに無いものは名前をそのまま出す。組織へエージェントを追加したとき、
# この表に足さなくても壊れないようにするため（拡張はファイル追加で済ませる）。
ROLE_NAMES = {
    "org-design": "設計",
    "org-implementation": "実装",
    "org-test": "テスト",
    "org-review": "レビュー",
    "org-documentation": "ドキュメント",
}

# タスク台帳の状態を、画面での並び順にする。動いているものを上へ。
STATE_ORDER = [
    "実装中", "テスト中", "レビュー中", "設計中", "テスト作成中",
    "PO確認待ち", "未着手", "保留", "完了", "中止",
]


def load_sibling(name: str, filename: str):
    """同じディレクトリにある別のスクリプトを、部品として読み込む。

    ファイル名にハイフンが入っていて `import` できないので、パスから直接
    読み込む。見つからない・壊れている場合は None を返し、モニタはその部分
    の表示を諦めて動き続ける（落とさない）。
    """
    path = os.path.join(HERE, filename)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (OSError, ImportError, SyntaxError, ValueError):
        return None


tokens = load_sibling("org_tokens", "org-tokens.py")
check = load_sibling("org_check", "org-check.py")


# --------------------------------------------------------------------------
# 会話記録を、前回の続きから読む
# --------------------------------------------------------------------------

class Tail:
    """1つのファイルを、前回読んだ位置の続きから読む。

    会話記録は書き込みの最中に読まれる。最後の1行が途中までしか書かれて
    いない状態は正常な出来事なので、その行は読まずに次回へ回す。
    """

    def __init__(self) -> None:
        self.offsets: dict = {}

    def read(self, path: str) -> list:
        start = self.offsets.get(path, 0)
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        if size < start:          # 記録が作り直された。最初から読み直す
            start = 0
        if size == start:
            return []
        try:
            with open(path, "rb") as f:
                f.seek(start)
                blob = f.read()
        except OSError:
            return []

        cut = blob.rfind(b"\n")   # 改行までが、書き終わっている範囲
        if cut < 0:
            return []
        self.offsets[path] = start + cut + 1

        out = []
        for line in blob[:cut].split(b"\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(entry, dict):
                out.append(entry)
        return out


def result_ids(entry: dict) -> list:
    """1つの行から、「道具の実行が終わった」印（tool_use_id）を取り出す。

    担当エージェントの起動も道具の呼び出しなので、その識別子がここに現れた
    ことが、そのエージェントが終わったという事実になる。
    """
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [
        block.get("tool_use_id")
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_result"
        and block.get("tool_use_id")
    ]


def empty_counts() -> dict:
    """費目（入力・出力・キャッシュ書込・キャッシュ読出）を0で用意する。"""
    return {kind: 0 for kind in (tokens.KINDS if tokens else [])}


def add_counts(into: dict, more: dict) -> None:
    for kind, value in more.items():
        into[kind] = into.get(kind, 0) + value


# --------------------------------------------------------------------------
# 状態を組み立てる
# --------------------------------------------------------------------------

class Watcher:
    """開発対象リポジトリの1セッションを見張り、画面に出す状態を組み立てる。

    HTTP の層からは切り離してある。状態の組み立てだけを検証できるようにする
    ため、また、画面を作り込む（ダッシュボード化する）ときにここを触らずに
    済ませるため。
    """

    def __init__(self, root: str, transcripts: str | None = None) -> None:
        self.root = os.path.abspath(root)
        self.transcripts = transcripts
        self.reset()
        self.last_scan = 0.0

    def reset(self) -> None:
        """見張る対象が変わったとき、数え直す。"""
        self.session: dict | None = None
        self.tail = Tail()
        self.main_counts = empty_counts()
        self.done: set = set()          # 終わった担当エージェントの識別子
        self.agents: dict = {}          # 識別子 -> 画面に出す1体分

    # --- 対象セッションを決める -------------------------------------------

    def pick_session(self, now: float) -> None:
        """このリポジトリで動いている、いちばん新しいセッションを選ぶ。

        探索は会話記録を総なめするので、毎回はやらない。
        """
        if tokens is None:
            return
        if self.session and now - self.last_scan < SESSION_RESCAN:
            return
        self.last_scan = now

        found = tokens.find_sessions(self.root, self.transcripts)
        if not found:
            return

        def freshness(session: dict) -> float:
            try:
                return os.path.getmtime(session["main"])
            except OSError:
                return 0.0

        newest = max(found, key=freshness)
        if not self.session or newest["id"] != self.session["id"]:
            self.reset()
            self.session = newest

    # --- 会話記録を読み進める ---------------------------------------------

    def scan_main(self) -> None:
        """メインセッション（オーケストレーター）の記録を読み進める。"""
        for entry in self.tail.read(self.session["main"]):
            for done_id in result_ids(entry):
                self.done.add(done_id)
            got = tokens.usage_of(entry)
            if got:
                add_counts(self.main_counts, got[1])

    def scan_agents(self) -> None:
        """担当エージェントの記録を読み進める。新しく現れた体も拾う。"""
        pattern = os.path.join(self.session["subagents"], "*.meta.json")
        for meta_path in sorted(glob.glob(pattern)):
            agent_id = os.path.basename(meta_path)[: -len(".meta.json")]
            log_path = meta_path[: -len(".meta.json")] + ".jsonl"
            agent = self.agents.get(agent_id)

            if agent is None:
                agent = self.start_record(agent_id, meta_path, log_path)
                if agent is None:
                    continue
                self.agents[agent_id] = agent

            if not agent["task"] and os.path.exists(log_path):
                # 指示文はエージェント起動の直後に書かれる。最初に覗いた
                # 時点ではまだ書かれていないことがあるので、取れるまで試す。
                agent["task"] = tokens.agent_task(log_path)

            for entry in self.tail.read(log_path):
                got = tokens.usage_of(entry)
                if got:
                    add_counts(agent["counts"], got[1])

            try:
                agent["last"] = os.path.getmtime(log_path)
            except OSError:
                pass

    def start_record(self, agent_id: str, meta_path: str, log_path: str):
        """新しく現れた担当エージェント1体分の入れ物を作る。"""
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f) or {}
            started = os.path.getmtime(meta_path)
        except (OSError, ValueError):
            return None

        role = meta.get("agentType") or "不明"
        return {
            "id": agent_id,
            "role": ROLE_NAMES.get(role, role),
            "agent_type": role,
            "description": meta.get("description") or "",
            "tool_use_id": meta.get("toolUseId") or "",
            "task": tokens.agent_task(log_path) if os.path.exists(log_path) else "",
            "started": started,
            "last": started,
            "counts": empty_counts(),
        }

    # --- タスク台帳 --------------------------------------------------------

    def read_tasks(self) -> tuple:
        """タスク台帳の索引を読む。読めなければ、その旨を伝える文言を返す。"""
        if check is None:
            return [], ["タスク台帳の読み取り部品（org-check.py）が見つからない"]
        try:
            path = check.find_index(self.root)
        except Exception as e:                      # 索引の CSV が複数ある等
            return [], [str(e)]
        if not path:
            return [], ["タスク台帳がまだ無い（組織が未着手）"]
        try:
            rows = check.read_index(path)
        except Exception as e:
            return [], ["タスク台帳を読めない: {}".format(e)]

        tasks = [{
            "id": row.get("ID", ""),
            "name": row.get("タスク名", ""),
            "state": row.get("状態", ""),
            "owner": row.get("担当", ""),
            "updated": row.get("更新日", ""),
        } for row in rows if row.get("ID")]

        def order(task: dict) -> tuple:
            state = task["state"]
            rank = STATE_ORDER.index(state) if state in STATE_ORDER else len(STATE_ORDER)
            return (rank, task["id"])

        return sorted(tasks, key=order), []

    # --- 画面へ渡す形にまとめる -------------------------------------------

    def state(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        notes = []

        if tokens is None:
            notes.append("会話記録の読み取り部品（org-tokens.py）が見つからない")
        else:
            self.pick_session(now)
            if self.session:
                self.scan_main()
                self.scan_agents()
            else:
                notes.append("このリポジトリの会話記録がまだ見つからない")

        tasks, task_notes = self.read_tasks()
        notes.extend(task_notes)

        agents = [self.view(agent, now) for agent in self.agents.values()]
        agents.sort(key=lambda a: (not a["running"], -a["started"]))

        total = dict(self.main_counts)
        for agent in self.agents.values():
            add_counts(total, agent["counts"])
        summary = {"合計": sum(total.values())}
        summary.update(total)

        return {
            "repo": os.path.basename(self.root),
            "root": self.root,
            "session": self.session["id"] if self.session else "",
            "generated": now,
            "refresh": REFRESH,
            "agents": agents,
            "running": sum(1 for a in agents if a["running"]),
            "tasks": tasks,
            "tokens": summary,
            "notes": notes,
        }

    def view(self, agent: dict, now: float) -> dict:
        """1体分を、画面が扱える形へ直す。"""
        running = bool(agent["tool_use_id"]) and agent["tool_use_id"] not in self.done
        end = now if running else max(agent["last"], agent["started"])
        return {
            "id": agent["id"],
            "role": agent["role"],
            "description": agent["description"],
            "task": agent["task"],
            "running": running,
            "started": agent["started"],
            "elapsed": max(0.0, end - agent["started"]),
            "tokens": sum(agent["counts"].values()),
        }


# --------------------------------------------------------------------------
# HTTP の層
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    """画面（/）と状態（/api/state）だけを返す。"""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:                       # noqa: N802（標準側の命名）
        self.server.last_seen = time.time()
        path = self.path.split("?")[0]
        if path == "/":
            self.send_body(load_page().encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            body = json.dumps(self.server.snapshot(), ensure_ascii=False)
            self.send_body(body.encode("utf-8"), "application/json; charset=utf-8")
        else:
            self.send_error(404)

    def send_body(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """2秒ごとのアクセスを、標準エラーへ延々と出さない。"""


class Monitor(ThreadingHTTPServer):
    """待ち受けと、状態の組み立ての間に立つ。

    画面を複数開かれても会話記録の読み直しが重ならないよう、鍵をかけて
    直前の結果を使い回す。
    """

    daemon_threads = True

    def __init__(self, address, watcher: Watcher) -> None:
        super().__init__(address, Handler)
        self.watcher = watcher
        self.lock = threading.Lock()
        self.cached: dict = {}
        self.cached_at = 0.0
        self.last_seen = time.time()

    def snapshot(self) -> dict:
        with self.lock:
            now = time.time()
            if not self.cached or now - self.cached_at >= MIN_REBUILD:
                self.cached = self.watcher.state(now)
                self.cached_at = now
            return self.cached


def probe_port(port: int, root: str, hits: dict) -> None:
    """1つのポートに、同じリポジトリを見ているモニタが居るか確かめる。"""
    url = "http://127.0.0.1:{}".format(port)
    try:
        with urllib.request.urlopen(url + "/api/state", timeout=PROBE_TIMEOUT) as res:
            state = json.loads(res.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return
    if isinstance(state, dict) and state.get("root") \
            and os.path.normcase(state["root"]) == os.path.normcase(root):
        hits[port] = url


def find_running(root: str, base: int, span: int) -> str | None:
    """同じリポジトリを見ているモニタが既に動いていないか探す。

    動いていれば、そのURLを返す。二重に立てず、既にあるものを開き直す。

    **候補のポートは同時に確かめる。** 環境によっては、空きポートへの接続が
    「すぐ拒否される」のではなく「待たされた末に時間切れになる」（Windows の
    ファイアウォールが黙って捨てる場合がこれ）。1つずつ順に試すと、その待ち
    時間が候補の数だけ積み上がり、セッションの開始が数秒止まる。
    """
    root = os.path.abspath(root)
    hits: dict = {}
    threads = [
        threading.Thread(target=probe_port, args=(port, root, hits), daemon=True)
        for port in range(base, base + span)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(PROBE_TIMEOUT + 0.5)
    for port in sorted(hits):
        return hits[port]
    return None


def serve(root: str, port: int, span: int, transcripts: str | None,
          idle: int, open_browser: bool) -> int:
    """モニタを立てて、待ち受けを始める。"""
    watcher = Watcher(root, transcripts)
    server = None
    for candidate in range(port, port + span):
        try:
            # 127.0.0.1 に限る。同じ機械の中からしか見えないようにするため
            server = Monitor(("127.0.0.1", candidate), watcher)
            break
        except OSError:
            continue
    if server is None:
        sys.stderr.write(
            "モニタ: ポート {}〜{} がすべて埋まっている。"
            "--port で別の番号を指定すること\n".format(port, port + span - 1)
        )
        return 2

    url = "http://127.0.0.1:{}".format(server.server_address[1])
    print("モニタ: {}  （画面を閉じて{}分で自動終了）".format(url, max(1, idle // 60)))
    sys.stdout.flush()

    if idle > 0:
        threading.Thread(target=watchdog, args=(server, idle), daemon=True).start()
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def watchdog(server: Monitor, idle: int) -> None:
    """画面から通信が来なくなったら、自分を終わらせる。"""
    while True:
        time.sleep(min(30, max(1, idle // 4)))
        if time.time() - server.last_seen > idle:
            server.shutdown()
            return


def spawn_detached(argv: list) -> bool:
    """自分を切り離した別プロセスとして起動する。

    フックは呼んだコマンドの終了を待つ。モニタは待ち受け続けるものなので、
    そのまま起動するとセッションの開始が止まってしまう。
    """
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        # 対話画面を持たない、親から独立したプロセスとして起動する
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        kwargs["creationflags"] = detached | new_group
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__)] + argv, **kwargs)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="稼働中の担当エージェント・着手中のタスク・トークン消費をブラウザで見せる"
    )
    parser.add_argument("--root", default=".", help="開発対象リポジトリの場所")
    parser.add_argument("--port", type=int, default=BASE_PORT, help="待ち受けるポート番号")
    parser.add_argument("--span", type=int, default=PORT_SPAN,
                        help="ポートが埋まっていたときに探す個数")
    parser.add_argument("--transcripts", help="会話記録の置き場（検証用に差し替える）")
    parser.add_argument("--idle-timeout", type=int, default=IDLE_TIMEOUT,
                        help="画面から通信が来なくなってから終了するまでの秒数（0で無効）")
    parser.add_argument("--no-open", action="store_true", help="ブラウザを開かない")
    parser.add_argument("--once", action="store_true", help="状態を1回 JSON で出して終わる")
    parser.add_argument("--hook", action="store_true", help="フックから呼ぶとき")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)

    if args.once:
        state = Watcher(root, args.transcripts).state()
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0 if state["session"] else 1

    if args.hook:
        return run_from_hook(args, root)

    return serve(root, args.port, args.span, args.transcripts,
                 args.idle_timeout, not args.no_open)


def run_from_hook(args, root: str) -> int:
    """フックから呼ばれたときの入口。何が起きても 0 で終わる。"""
    if check is None or not check.find_index(root):
        return 0                   # 組織を動かしていないセッション。何もしない

    running = find_running(root, args.port, args.span)
    if running:
        if not args.no_open:
            webbrowser.open(running)
        print("モニタ: {} （既に動いているものを開いた）".format(running))
        return 0

    child = ["--root", root, "--port", str(args.port), "--span", str(args.span),
             "--idle-timeout", str(args.idle_timeout)]
    if args.transcripts:
        child += ["--transcripts", args.transcripts]
    if args.no_open:
        child.append("--no-open")

    if spawn_detached(child):
        print("モニタ: http://127.0.0.1:{} 付近で起動した".format(args.port))
    return 0


# --------------------------------------------------------------------------
# 画面
# --------------------------------------------------------------------------

# 画面は、このスクリプトと同じディレクトリに置いた HTML ファイルである。
# 外部の配信サーバ（CDN）からは何も読み込まない——配布物にネットワーク依存を
# 持ち込まないため。オフラインでも、初回でも、そのまま表示できる。
PAGE_FILE = os.path.join(HERE, "org-monitor-page.html")

# 画面のファイルが見つからないときに出すもの。取得口（/api/state）は生きて
# いるので、何が足りないかを伝えて終わりにする。
FALLBACK_PAGE = """<!doctype html><meta charset="utf-8">
<title>AI開発組織モニタ</title>
<body style="font-family:sans-serif;padding:2em;line-height:1.7">
<h1>画面のファイルが見つからない</h1>
<p>モニタ本体（<code>org-monitor.py</code>）と同じ場所に
<code>org-monitor-page.html</code> を置くこと。配布物では2つで1組である。</p>
<p>状態そのものは <a href="/api/state">/api/state</a> から読める。</p>
</body>
"""


def load_page() -> str:
    """画面のファイルを読む。要求のたびに読み直す。

    毎回読み直すのは、画面を作り込むときに、書き換えたらそのまま再読み込み
    で確かめられるようにするため。読むのは画面を開いたときだけで、2秒ごとの
    状態の取得では読まない。
    """
    try:
        with open(PAGE_FILE, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return FALLBACK_PAGE




if __name__ == "__main__":
    sys.exit(main())
