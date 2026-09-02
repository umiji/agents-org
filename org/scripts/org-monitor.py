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
                   の行として現れたら、そのターンぶんの結果を返したという
                   ことなので「待機」へ移す。逆に、その体自身の記録が伸びた
                   ら「稼働中」へ移す。**状態は保持し、証拠があったときだけ
                   動かす**（巡回のたびに計算し直すと、根拠を失った瞬間に
                   全部が稼働中へ戻ってしまう）。

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
    python3 org-monitor.py --root ... --session-file X 見張るセッションを名指しする

`python3` という名前のコマンドが無い環境（Windows の標準的な導入ではこれが
普通）では `python` に読み替える。

--hook を付けたときの振る舞い:
    1. タスク台帳が無ければ、何もせずに終わる（組織を動かしていない普通の
       セッションでブラウザが開くのを防ぐ）
    2. 同じリポジトリのモニタが既に動いていれば、何もしない。そのURLを
       1行だけ知らせる（ブラウザは開かない。開くのは初回だけ）
    3. 立てる場合は、自分を切り離した別プロセスとして起動し、すぐ戻る
       （フックは呼んだコマンドの終了を待つので、待たせない）
    4. そのとき、**いま始まったセッションの記録の場所をフックから受け取り、
       立てるモニタへ渡す。** フックは呼ばれるときに、そのセッションの情報
       （識別子・記録の場所・作業ディレクトリ）を JSON で標準入力へ渡して
       くるので、そこから取る。**これが無いと、モニタは「記録がいちばん
       新しく更新されたセッション」を選ぶしかなく、立ち上がる瞬間にまだ
       新しい記録が書かれていなければ、古いセッションを選んで固定して
       しまう**（そのモニタは何も映さないまま動き続ける）

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

# Windows のコンソールや、呼び出し元がパイプで受け取る場面では、Python の
# 既定の出力文字コードが cp932 になる。この組織の出力は日本語で、記号（em
# dash 等）を含むため、そのままだと UnicodeEncodeError で**処理そのものが
# 落ちる**。落ちたことはセッション開始フックの中では見えないので、
# 「走っているつもりで走っていない」状態になる。
# 呼び出し側（フック、パイプ、CI）はいずれも UTF-8 で読むため、出力を
# UTF-8 に固定する。文字化けと異常終了の両方がこれで消える。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 差し替え済み / 閉じている場合
        pass


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

# エージェント定義の名前を、画面に出す短い呼び名へ直す。
# ここに無いものは名前をそのまま出す。組織へエージェントを追加したとき、
# この表に足さなくても壊れないようにするため（拡張はファイル追加で済ませる）。
ROLE_NAMES = {
    "org-design": "設計",
    "org-implementation": "実装",
    "org-test": "テスト",
    "org-review": "レビュー",
    "org-documentation": "ドキュメント",
    "org-improvement": "改善",
}

# タスク台帳の状態を、画面での並び順にする。動いているものを上へ。
STATE_ORDER = [
    "実装中", "テスト中", "レビュー中", "設計中", "テスト作成中",
    "PO確認待ち", "未着手", "保留", "完了", "中止",
]


def hook_input() -> dict:
    """フックが標準入力へ渡してくる JSON を読む。読めなければ空で返す。

    Claude Code は、フックを呼ぶときに、そのセッションの識別子・会話記録の
    場所・作業ディレクトリを JSON にして標準入力へ流す。**ここから記録の場所
    が取れると、見張る相手を推測ではなく名指しで決められる。**

    人が手で叩いたときは端末が標準入力につながっている。その場合は**読みに
    行かない**——読むと、入力を待って止まってしまうため。
    """
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    try:
        got = json.loads(raw)
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def session_from_file(path: str) -> dict | None:
    """会話記録のパス1本から、見張る相手の情報を組み立てる。

    ファイルがまだ存在しなくてよい。**セッションが始まった直後は、記録が
    まだ書かれていないのが普通である。** 読み進める側は、無いファイルを
    「まだ何も無い」として扱えるようにしてある。
    """
    if not path or not path.endswith(".jsonl"):
        return None
    session_id = os.path.basename(path)[: -len(".jsonl")]
    if not session_id:
        return None
    return {
        "id": session_id,
        "main": path,
        "subagents": os.path.join(os.path.dirname(path), session_id, "subagents"),
    }


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

    def __init__(self, root: str, transcripts: str | None = None,
                 session_file: str | None = None) -> None:
        self.root = os.path.abspath(root)
        self.transcripts = transcripts
        # 見張る相手が名指しされているなら、探さずにそれを使う。
        self.session_file = session_file
        # 覚えたものを捨てて数え直す手段は、用意しない。**途中で見張る相手を
        # 変えない**と決めたので、捨てる場面が存在しないためである。捨てられ
        # る作りにしておくと、そこが必ず事故の入口になる（実際、見張る相手を
        # 選び直すたびに全部捨てていたのが、表示が数秒で消える原因だった）。
        self.session: dict | None = None
        self.tail = Tail()
        self.main_counts = empty_counts()
        self.done: set = set()          # 結果が返った担当エージェントの識別子
        self.agents: dict = {}          # 識別子 -> 画面に出す1体分

    # --- 対象セッションを決める -------------------------------------------

    def pick_session(self) -> None:
        """見張るセッションを1つ決める。**一度決めたら、二度と変えない。**

        ここでいうセッションは「ウィンドウを開いてから閉じるまで」であり、
        会話記録のファイル1本がこれ1つに対応する。プロンプト1往復（ターン）
        とは別のものである。

        以前は一定間隔で「記録がいちばん新しく更新されたセッション」を選び
        直していた。**複数のウィンドウで同じリポジトリを開いていると、その
        選択が行ったり来たりし、乗り換えのたびに覚えたものを全部捨てていた。**
        表示中のエージェントが数秒で消える・終わった体が根拠なく稼働中へ
        戻る・一覧が丸ごと消える、はすべてここから出ていた。

        引き換えに、**別のウィンドウで動いている担当エージェントは見えない。**
        PO はこれを了解のうえでこの形を選んでいる（決定 2026-09-02）。

        **相手が名指しされているなら、探さない。** セッション開始のフックが
        「いま始まったセッションの記録の場所」を渡してくる場合がそれで、
        推測が入らないぶん確実である。名指しが無いのは、人が手で立ち上げた
        ときであり、そのときだけ「いちばん新しいもの」を選ぶ。
        """
        if tokens is None or self.session is not None:
            return

        named = session_from_file(self.session_file) if self.session_file else None
        if named:
            self.session = named
            return

        found = tokens.find_sessions(self.root, self.transcripts)
        if not found:
            return          # まだ記録が無い。次の巡回で改めて探す

        def freshness(session: dict) -> float:
            try:
                return os.path.getmtime(session["main"])
            except OSError:
                return 0.0

        self.session = max(found, key=freshness)

    # --- 会話記録を読み進める ---------------------------------------------

    def scan_main(self, now: float) -> None:
        """メインセッション（オーケストレーター）の記録を読み進める。

        ここで拾う「実行結果」の行が、**その担当エージェントがそのターンぶん
        の結果を返した**という唯一の証拠である。証拠が来た体だけを待機へ移す。
        """
        for entry in self.tail.read(self.session["main"]):
            for done_id in result_ids(entry):
                self.done.add(done_id)
                self.mark_idle(done_id, now)
            got = tokens.usage_of(entry)
            if got:
                add_counts(self.main_counts, got[1])

    def scan_agents(self, now: float) -> None:
        """担当エージェントの記録を読み進める。新しく現れた体も拾う。

        **記録が伸びたことを、その体が動いている証拠として使う。** ファイル
        の更新時刻は使わない——中身が1行も増えていなくても、触られただけで
        進むためである（待機中の体の経過時間が伸び続ける原因になっていた）。
        """
        pattern = os.path.join(self.session["subagents"], "*.meta.json")
        for meta_path in sorted(glob.glob(pattern)):
            agent_id = os.path.basename(meta_path)[: -len(".meta.json")]
            log_path = meta_path[: -len(".meta.json")] + ".jsonl"
            agent = self.agents.get(agent_id)

            # 初めて見つけた体は、そこまでの記録をまとめて読むことになる。
            # **この読み込みは「いま動いている証拠」ではない**ので、状態を
            # 動かさない（初期の状態は start_record が決める）。
            first_sight = agent is None
            if first_sight:
                agent = self.start_record(agent_id, meta_path, log_path)
                if agent is None:
                    continue
                self.agents[agent_id] = agent

            if not agent["task"] and os.path.exists(log_path):
                # 指示文はエージェント起動の直後に書かれる。最初に覗いた
                # 時点ではまだ書かれていないことがあるので、取れるまで試す。
                agent["task"] = tokens.agent_task(log_path)

            grew = False
            for entry in self.tail.read(log_path):
                grew = True
                got = tokens.usage_of(entry)
                if got:
                    add_counts(agent["counts"], got[1])

            if grew and not first_sight:
                self.mark_running(agent, now)

    def start_record(self, agent_id: str, meta_path: str, log_path: str):
        """新しく現れた担当エージェント1体分の入れ物を作る。"""
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f) or {}
            started = os.path.getmtime(meta_path)
        except (OSError, ValueError):
            return None

        role = meta.get("agentType") or "不明"
        tool_use_id = meta.get("toolUseId") or ""
        # 見つけた時点で既に結果が返っている（＝モニタを立てる前に終わって
        # いた）ことがある。その場合は最初から待機で置く。
        running = bool(tool_use_id) and tool_use_id not in self.done
        return {
            "id": agent_id,
            "role": ROLE_NAMES.get(role, role),
            "agent_type": role,
            "description": meta.get("description") or "",
            "tool_use_id": tool_use_id,
            "log": log_path,
            "task": tokens.agent_task(log_path) if os.path.exists(log_path) else "",
            "started": started,
            "running": running,
            "active_since": started if running else None,
            "elapsed": 0.0,          # 閉じた区間の合計（秒）
            "counts": empty_counts(),
        }

    # --- 状態の遷移 --------------------------------------------------------
    #
    # 状態は2つしか持たない。
    #
    #   稼働中   いま動いている（自分の記録が伸びた）
    #   待機     そのターンぶんの結果は返した。**呼べば文脈を保ったまま続き
    #            ができる**ので、終わったわけではない
    #
    # 「終了」という状態は持たない。担当エージェントを名指しで閉じる手段が
    # 無く、同じセッションのエージェントは必ず全部同時に終わるため、終了は
    # エージェント1体ごとの状態ではなく**セッションの属性**だからである。
    # そしてこのモニタは見張っている1セッションしか映さないので、映っている
    # 間はそのセッションが生きている。
    #
    # **遷移は、証拠があったときだけ起こす。**

    def mark_running(self, agent: dict, now: float) -> None:
        """記録が伸びた＝動いている。待機からの再開なら、新しい区間を開く。"""
        if agent["running"]:
            return
        agent["running"] = True
        agent["active_since"] = now

    def mark_idle(self, tool_use_id: str, now: float) -> None:
        """結果が返った＝待機へ移る。開いていた区間を閉じて足し込む。"""
        for agent in self.agents.values():
            if agent["tool_use_id"] != tool_use_id or not agent["running"]:
                continue
            start = agent["active_since"]
            agent["running"] = False
            agent["active_since"] = None
            if start is not None:
                end = min(self.wrote_last(agent, now), now)
                agent["elapsed"] += max(0.0, end - start)

    def wrote_last(self, agent: dict, now: float) -> float:
        """区間を閉じる時刻。その体の記録に最後に書かれた時刻を使う。

        更新時刻をここで**1回だけ**読むのは、閉じた区間はもう動かないから
        である。毎回読み直すと、ファイルが触られるたびに待機中の体の経過時間
        が伸びる（これが「終了した欄に入っているのに時間が伸びる」の原因）。
        """
        try:
            return os.path.getmtime(agent["log"])
        except OSError:
            return now

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
            self.pick_session()
            if self.session:
                # **順番に意味がある。** 先に「記録が伸びた体」を稼働中にし、
                # そのあとで「結果が返った体」を待機へ移す。逆にすると、結果
                # が返ったのと同じ巡回で読んだ末尾の数行が、待機へ移した体を
                # そのまま稼働中へ押し戻してしまう。
                self.scan_agents(now)
                self.scan_main(now)
                if not os.path.exists(self.session["main"]):
                    # 名指しで見張っている相手の記録が、まだ書かれていない。
                    # セッションが始まった直後は普通のことなので、異常では
                    # なく「まだ何も無い」と伝える。
                    notes.append(
                        "見張るセッションの記録がまだ読めない（始まった直後なら"
                        "そのうち出る）: {}".format(self.session["main"])
                    )
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
        """1体分を、画面が扱える形へ直す。

        **ここで状態を計算し直さない。** 持っているものをそのまま出す。
        経過時間は「閉じた区間の合計 ＋ いま開いている区間」であり、待機中の
        体には開いている区間が無いので、それ以上は伸びない。
        """
        elapsed = agent["elapsed"]
        if agent["running"] and agent["active_since"] is not None:
            elapsed += max(0.0, now - agent["active_since"])
        return {
            "id": agent["id"],
            "role": agent["role"],
            "description": agent["description"],
            "task": agent["task"],
            "running": agent["running"],
            "started": agent["started"],
            "elapsed": max(0.0, elapsed),
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
          idle: int, open_browser: bool, session_file: str | None = None) -> int:
    """モニタを立てて、待ち受けを始める。"""
    watcher = Watcher(root, transcripts, session_file)
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
    parser.add_argument("--session-file",
                        help="見張るセッションの会話記録（フックから渡される）")
    parser.add_argument("--idle-timeout", type=int, default=IDLE_TIMEOUT,
                        help="画面から通信が来なくなってから終了するまでの秒数（0で無効）")
    parser.add_argument("--no-open", action="store_true", help="ブラウザを開かない")
    parser.add_argument("--once", action="store_true", help="状態を1回 JSON で出して終わる")
    parser.add_argument("--hook", action="store_true", help="フックから呼ぶとき")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)

    if args.once:
        state = Watcher(root, args.transcripts, args.session_file).state()
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0 if state["session"] else 1

    if args.hook:
        return run_from_hook(args, root)

    return serve(root, args.port, args.span, args.transcripts,
                 args.idle_timeout, not args.no_open, args.session_file)


def run_from_hook(args, root: str) -> int:
    """フックから呼ばれたときの入口。何が起きても 0 で終わる。"""
    if check is None or not check.find_index(root):
        return 0                   # 組織を動かしていないセッション。何もしない

    # **いま始まったセッションの記録の場所を、フックの入力から取る。**
    # 取れれば、立てるモニタは相手を推測せずに済む。取れなくても動く
    # （そのときは「いちばん新しい記録」を選ぶ、これまでの形になる）。
    session_file = args.session_file or hook_input().get("transcript_path") or ""

    running = find_running(root, args.port, args.span)
    if running:
        # **開き直さない。** セッションを開くたびに同じタブが立ち上がるのが
        # 煩わしい、という報告があったため。URL を1行出しておけば、フックの
        # 出力はセッションの文脈へ入るので見失わない。開くのは初回だけになる。
        print("モニタ: {} （既に動いている。ブラウザは開かない）".format(running))
        return 0

    child = ["--root", root, "--port", str(args.port), "--span", str(args.span),
             "--idle-timeout", str(args.idle_timeout)]
    if args.transcripts:
        child += ["--transcripts", args.transcripts]
    if session_file:
        child += ["--session-file", session_file]
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
