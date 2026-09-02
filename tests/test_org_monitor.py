#!/usr/bin/env python3
"""org/scripts/org-monitor.py の検証。

実行:
    python3 tests/test_org_monitor.py        （`python3` が無ければ `python`）

標準ライブラリの unittest だけを使う。検証対象のスクリプトが「追加インストール
を要求しない」という制約で書かれているため、その検証も同じ条件で走れないと、
配布先で確かめられない。

ここで確かめるのは、机上で正しさを議論しにくい7点である。

  1. 担当エージェントの状態（稼働中／待機）の遷移。起動を識別する番号
     （toolUseId）が実行結果としてメインセッションの記録に現れたら待機へ、
     その体自身の記録が伸びたら稼働中へ——この判定が、画面の中心になる
  2. 会話記録を「前回の続きから」読むこと。2秒ごとに全部読み直すと数メガ
     バイトの読み込みが繰り返され、更新に耐えない。かつ二重計上してはいけない
  3. 書き込みの途中の行（改行がまだ来ていない行）で壊れないこと。会話記録は
     セッションが動いている最中に読まれるので、これは正常な出来事である
  4. タスク台帳の読み取りと並び順。動いているタスクが上に来ること
  5. 台帳や会話記録が無い・壊れている場合に、落ちずに理由を伝えること
  6. **経過時間を区間で持つこと。** 待機へ移ったらそこで止まり、記録ファイル
     を触られても伸びない。再開したら新しい区間が開く
  7. **見張るセッションを乗り換えないこと。** 別のウィンドウで新しいセッション
     が始まっても相手を変えず、覚えたものを捨てない
  8. **見張る相手を名指しで受け取れること。** セッション開始のフックが渡してくる
     「いま始まったセッションの記録の場所」を使う。**これが無いと「いちばん新しい
     記録」を選ぶしかなく、立ち上がる瞬間に新しい記録がまだ書かれていなければ、
     古いセッションを選んで固定してしまう**（何も映さないまま動き続ける）

6と7は、実際に出た不具合（表示が数秒で消える／待機のはずの体の時間が伸び
続ける／一覧が丸ごと消える）に直接対応している。**症状はすべて「乗り換えの
たびに覚えたものを全部捨てていた」ことから出ていた。**

画面（HTML）と待ち受け（HTTPサーバ）は検証しない。状態の組み立てさえ正しけ
れば、そこは表示するだけの層である。
"""

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "..", "org", "scripts", "org-monitor.py")

# ファイル名にハイフンが入っていて `import` できないので、パスから直接読み込む。
_spec = importlib.util.spec_from_file_location("org_monitor", TARGET)
om = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(om)


# --------------------------------------------------------------------------
# 擬似的な会話記録を組み立てる道具
# --------------------------------------------------------------------------

def usage_entry(model="claude-opus-5", inp=10, out=5, write=2, read=3):
    """モデルの返答1回分。トークンの内訳が入っている行。"""
    return {
        "type": "assistant",
        "message": {
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_creation_input_tokens": write,
                "cache_read_input_tokens": read,
            },
        },
    }


def said(text, cwd=None):
    """人間（またはオーケストレーター）が書いた入力1行。"""
    entry = {"type": "user", "message": {"content": text}}
    if cwd:
        entry["cwd"] = cwd
    return entry


def finished(tool_use_id):
    """担当エージェントの起動が終わった、という実行結果の行。"""
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id}]},
    }


def write_lines(path, entries, newline_at_end=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)
    if not newline_at_end and body.endswith("\n"):
        body = body[:-1]
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


def append_lines(path, entries, newline_at_end=True):
    body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)
    if not newline_at_end and body.endswith("\n"):
        body = body[:-1]
    with open(path, "a", encoding="utf-8") as f:
        f.write(body)


class Fixture:
    """開発対象リポジトリと、その会話記録を1式でっち上げる。"""

    def __init__(self, base, session_id="S-1"):
        self.root = os.path.join(base, "repo")
        self.transcripts = os.path.join(base, "projects")
        self.session_id = session_id
        project = os.path.join(self.transcripts, "encoded-name")
        self.main = os.path.join(project, session_id + ".jsonl")
        self.subagents = os.path.join(project, session_id, "subagents")
        os.makedirs(self.root)
        os.makedirs(self.subagents)
        # 先頭に作業ディレクトリの行を置く。置き場の特定はディレクトリ名では
        # なく、この cwd の突き合わせで行われる。
        write_lines(self.main, [said("開始", cwd=self.root)])

    def main_add(self, entries, newline_at_end=True):
        append_lines(self.main, entries, newline_at_end)

    def agent(self, agent_id, tool_use_id, agent_type, description,
              instruction, entries=()):
        """担当エージェント1体分の記録を作る。"""
        meta = os.path.join(self.subagents, agent_id + ".meta.json")
        with open(meta, "w", encoding="utf-8") as f:
            json.dump({"agentType": agent_type, "description": description,
                       "toolUseId": tool_use_id, "spawnDepth": 1}, f)
        log = os.path.join(self.subagents, agent_id + ".jsonl")
        write_lines(log, [said(instruction)] + list(entries))
        return log

    def open_another_session(self, session_id, older_by=60.0):
        """**同じリポジトリで、別のウィンドウがもう1つ開いた**状態を作る。

        新しい方の記録を新しい更新時刻にし、既に見張っている方を古くする。
        こうしておかないと「いちばん新しいものを選ぶ」が働かず、乗り換えを
        しないことの検証にならない。
        """
        project = os.path.dirname(self.main)
        main = os.path.join(project, session_id + ".jsonl")
        subagents = os.path.join(project, session_id, "subagents")
        os.makedirs(subagents, exist_ok=True)
        write_lines(main, [said("開始", cwd=self.root)])

        now = time.time()
        os.utime(self.main, (now - older_by, now - older_by))
        os.utime(main, (now, now))
        return main, subagents

    def ledger(self, rows):
        """タスク台帳の索引（CSV）を置く。"""
        docs = os.path.join(self.root, "docs")
        os.makedirs(docs, exist_ok=True)
        path = os.path.join(docs, "task-list-repo.csv")
        header = "ID,作成日,更新日,タスク名,状態,優先度,担当,依存タスク,ドキュメント"
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(header + "\n")
            for row in rows:
                f.write(",".join(row) + "\n")
        return path

    def watcher(self):
        return om.Watcher(self.root, self.transcripts)


class MonitorTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="org-monitor-test-")
        self.fx = Fixture(self.base)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    # ---------------------------------------------------------------- 1

    def test_稼働中と待機を分ける(self):
        """実行結果の行が現れた体だけが「待機」になる。

        待機は「終了」ではない。**呼べば文脈を保ったまま続きができる。**
        """
        self.fx.agent("agent-aaa", "toolu_A", "org-implementation",
                      "認証APIの実装", "T-007 の実装を行うこと",
                      [usage_entry(out=100)])
        self.fx.agent("agent-bbb", "toolu_B", "org-review",
                      "台帳整合の確認", "T-005 のレビューを行うこと",
                      [usage_entry(out=20)])
        # 片方だけ終わった、という事実をメインセッションへ書く
        self.fx.main_add([finished("toolu_A")])

        state = self.fx.watcher().state()
        by_task = {a["task"]: a for a in state["agents"]}

        self.assertFalse(by_task["T-007"]["running"], "実行結果が来た体は待機")
        self.assertTrue(by_task["T-005"]["running"], "実行結果が無い体は稼働中")
        self.assertEqual(state["running"], 1)

    def test_稼働中の体が先に並ぶ(self):
        self.fx.agent("agent-aaa", "toolu_A", "org-design", "設計", "T-001 の設計")
        self.fx.agent("agent-bbb", "toolu_B", "org-test", "テスト", "T-002 のテスト")
        self.fx.main_add([finished("toolu_A")])

        agents = self.fx.watcher().state()["agents"]
        self.assertTrue(agents[0]["running"])
        self.assertEqual(agents[0]["task"], "T-002")

    def test_担当名と作業内容とタスクIDを拾う(self):
        self.fx.agent("agent-aaa", "toolu_A", "org-implementation",
                      "認証APIの実装", "T-007 の実装を行うこと")
        agent = self.fx.watcher().state()["agents"][0]

        self.assertEqual(agent["role"], "実装", "定義名を短い呼び名へ直す")
        self.assertEqual(agent["description"], "認証APIの実装")
        self.assertEqual(agent["task"], "T-007")

    def test_知らないエージェント定義でも名前をそのまま出す(self):
        """組織へエージェントを1つ足したとき、モニタ側の表を直さなくても壊れない。"""
        self.fx.agent("agent-zzz", "toolu_Z", "org-security",
                      "脆弱性の確認", "T-009 の確認")
        agent = self.fx.watcher().state()["agents"][0]
        self.assertEqual(agent["role"], "org-security")

    # ---------------------------------------------------------------- 2

    def test_追記分だけを読み足して二重計上しない(self):
        """2秒ごとに呼ばれる前提。全部読み直すと重く、二重に数えると嘘になる。"""
        log = self.fx.agent("agent-aaa", "toolu_A", "org-implementation",
                            "実装", "T-007 の実装", [usage_entry(out=100)])
        watcher = self.fx.watcher()

        first = watcher.state()["agents"][0]["tokens"]
        self.assertEqual(first, 10 + 100 + 2 + 3)

        # 読み直しただけでは増えない
        self.assertEqual(watcher.state()["agents"][0]["tokens"], first)

        # 追記した分だけ増える
        append_lines(log, [usage_entry(out=50)])
        self.assertEqual(watcher.state()["agents"][0]["tokens"], first + 10 + 50 + 2 + 3)

    def test_合計はオーケストレーターと担当エージェントの両方を足す(self):
        self.fx.main_add([usage_entry(out=7)])
        self.fx.agent("agent-aaa", "toolu_A", "org-implementation",
                      "実装", "T-007 の実装", [usage_entry(out=11)])

        total = self.fx.watcher().state()["tokens"]
        self.assertEqual(total["出力"], 7 + 11)
        self.assertEqual(total["合計"], sum(v for k, v in total.items() if k != "合計"))

    # ---------------------------------------------------------------- 3

    def test_書きかけの行は次回に回す(self):
        """改行がまだ来ていない行を読むと、壊れた JSON を拾う。そこで止めておく。"""
        path = os.path.join(self.base, "partial.jsonl")
        write_lines(path, [usage_entry(out=1), usage_entry(out=2)],
                    newline_at_end=False)

        tail = om.Tail()
        first = tail.read(path)
        self.assertEqual(len(first), 1, "書き終わっている1行だけを読む")

        # 残りが書き終わったら、次の呼び出しで読める
        append_lines(path, [])
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n")
        self.assertEqual(len(tail.read(path)), 1)

    def test_壊れた行は飛ばして残りを読む(self):
        path = os.path.join(self.base, "broken.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ここは壊れている\n")
            f.write(json.dumps(usage_entry(out=9)) + "\n")

        entries = om.Tail().read(path)
        self.assertEqual(len(entries), 1)

    def test_記録が作り直されたら最初から読み直す(self):
        path = os.path.join(self.base, "reset.jsonl")
        write_lines(path, [usage_entry(out=1), usage_entry(out=2)])
        tail = om.Tail()
        self.assertEqual(len(tail.read(path)), 2)

        write_lines(path, [usage_entry(out=3)])      # 短くなった＝作り直された
        self.assertEqual(len(tail.read(path)), 1)

    def test_改善エージェントの呼び名が出る(self):
        """6体目（改善）が対応表に入っていること。抜けていると生の定義名が出る。"""
        self.fx.agent("agent-imp", "toolu_I", "org-improvement",
                      "運用課題の整理", "T-020 の整理")
        self.assertEqual(self.fx.watcher().state()["agents"][0]["role"], "改善")

    def test_モニタを立てる前に終わっていた体は最初から待機(self):
        """実行結果を先に読み、あとからその体を見つけた場合。"""
        self.fx.main_add([finished("toolu_A")])
        watcher = self.fx.watcher()
        watcher.state()                       # ここで実行結果の行だけを読む

        self.fx.agent("agent-aaa", "toolu_A", "org-implementation",
                      "実装", "T-007 の実装")
        self.assertFalse(watcher.state()["agents"][0]["running"])

    # ---------------------------------------------------------------- 6

    def test_待機へ移った体の経過時間は伸びない(self):
        """報告された症状そのもの。待機の欄に入っているのに時間が伸びていた。"""
        self.fx.agent("agent-aaa", "toolu_A", "org-implementation",
                      "実装", "T-007 の実装")
        self.fx.main_add([finished("toolu_A")])
        watcher = self.fx.watcher()

        base = time.time()
        first = watcher.state(base)["agents"][0]["elapsed"]
        later = watcher.state(base + 3600)["agents"][0]["elapsed"]
        self.assertEqual(first, later, "1時間経っても待機中の体の時間は動かない")

    def test_記録を触っただけでは経過時間が伸びない(self):
        """区間の終わりを、ファイルの更新時刻から毎回取り直してはいけない。"""
        log = self.fx.agent("agent-aaa", "toolu_A", "org-implementation",
                            "実装", "T-007 の実装")
        self.fx.main_add([finished("toolu_A")])
        watcher = self.fx.watcher()

        base = time.time()
        first = watcher.state(base)["agents"][0]["elapsed"]

        touched = base + 5000                 # 中身は増えていないが、触られた
        os.utime(log, (touched, touched))
        self.assertEqual(watcher.state(base + 6000)["agents"][0]["elapsed"], first)

    def test_稼働中の体の経過時間は伸びる(self):
        self.fx.agent("agent-aaa", "toolu_A", "org-implementation",
                      "実装", "T-007 の実装")
        watcher = self.fx.watcher()

        base = time.time()
        first = watcher.state(base)["agents"][0]["elapsed"]
        later = watcher.state(base + 60)["agents"][0]["elapsed"]
        self.assertAlmostEqual(later - first, 60, delta=1.0)

    def test_記録が伸びたら待機から稼働中へ戻る(self):
        """担当エージェントは文脈を保ったまま再開できる。**再開を隠さない。**"""
        log = self.fx.agent("agent-aaa", "toolu_A", "org-implementation",
                            "実装", "T-007 の実装")
        self.fx.main_add([finished("toolu_A")])
        watcher = self.fx.watcher()
        self.assertFalse(watcher.state()["agents"][0]["running"])

        append_lines(log, [usage_entry(out=5)])
        self.assertTrue(watcher.state()["agents"][0]["running"],
                        "呼び直されて記録が伸びたら、稼働中へ戻る")

    def test_同じ巡回で記録が伸びて結果も返ったら待機になる(self):
        """末尾の数行を読んだことが、待機へ移した体を押し戻してはいけない。"""
        log = self.fx.agent("agent-aaa", "toolu_A", "org-implementation",
                            "実装", "T-007 の実装")
        watcher = self.fx.watcher()
        self.assertTrue(watcher.state()["agents"][0]["running"])

        append_lines(log, [usage_entry(out=5)])
        self.fx.main_add([finished("toolu_A")])
        self.assertFalse(watcher.state()["agents"][0]["running"])

    # ---------------------------------------------------------------- 7

    def test_別のセッションが始まっても見張る相手を変えない(self):
        """乗り換えが、覚えたものを全部捨てる事故の入口だった。"""
        self.fx.agent("agent-aaa", "toolu_A", "org-implementation",
                      "実装", "T-007 の実装", [usage_entry(out=100)])
        watcher = self.fx.watcher()
        first = watcher.state()
        self.assertEqual(first["session"], "S-1")
        self.assertEqual(len(first["agents"]), 1)

        # 別のウィンドウが開き、そちらでもエージェントが動き出した
        _, subagents = self.fx.open_another_session("S-2")
        meta = os.path.join(subagents, "agent-zzz.meta.json")
        with open(meta, "w", encoding="utf-8") as f:
            json.dump({"agentType": "org-review", "description": "別窓のレビュー",
                       "toolUseId": "toolu_Z", "spawnDepth": 1}, f)

        after = watcher.state()
        self.assertEqual(after["session"], "S-1", "相手を乗り換えない")
        self.assertEqual([a["id"] for a in after["agents"]], ["agent-aaa"],
                         "別窓の体は映らない。こちらの体も消えない")
        self.assertEqual(after["agents"][0]["tokens"],
                         first["agents"][0]["tokens"], "数え直さない")

    # ---------------------------------------------------------------- 8

    def test_名指しされたセッションを見張る(self):
        """記録の更新時刻がどうであれ、名指しが勝つ。"""
        other_main, _ = self.fx.open_another_session("S-2")   # こちらの方が新しい
        watcher = om.Watcher(self.fx.root, self.fx.transcripts,
                             session_file=self.fx.main)

        self.assertEqual(watcher.state()["session"], "S-1",
                         "新しい方ではなく、名指しされた方を見る")

    def test_名指しが無ければいちばん新しいものを選ぶ(self):
        """人が手で立ち上げたときは名指しが無い。これまでどおりに動く。"""
        self.fx.open_another_session("S-2")
        self.assertEqual(self.fx.watcher().state()["session"], "S-2")

    def test_名指しされた記録がまだ書かれていなくても待てる(self):
        """セッションが始まった直後は、記録がまだ無いのが普通である。"""
        project = os.path.dirname(self.fx.main)
        yet = os.path.join(project, "S-9.jsonl")
        watcher = om.Watcher(self.fx.root, self.fx.transcripts, session_file=yet)

        state = watcher.state()
        self.assertEqual(state["session"], "S-9", "無くても相手は決まっている")
        self.assertEqual(state["agents"], [])
        self.assertTrue(any("まだ読めない" in note for note in state["notes"]))

        # 書かれ始めたら、そのまま読み進める
        write_lines(yet, [said("開始", cwd=self.fx.root), usage_entry(out=42)])
        self.assertEqual(watcher.state()["tokens"]["出力"], 42)

    def test_フックの入力から会話記録の場所を読む(self):
        got = self.read_hook_input(json.dumps({
            "session_id": "S-1",
            "transcript_path": "/somewhere/S-1.jsonl",
            "cwd": self.fx.root,
        }))
        self.assertEqual(got.get("transcript_path"), "/somewhere/S-1.jsonl")

    def test_フックの入力が壊れていても空で返す(self):
        """入力が無い・壊れているだけで、セッションの開始を止めてはいけない。"""
        self.assertEqual(self.read_hook_input(""), {})
        self.assertEqual(self.read_hook_input("{ここは壊れている"), {})
        self.assertEqual(self.read_hook_input('"文字列であって表ではない"'), {})

    def read_hook_input(self, text):
        """標準入力を差し替えて、フックの入力の読み取りだけを試す。"""
        real = om.sys.stdin
        om.sys.stdin = io.StringIO(text)
        try:
            return om.hook_input()
        finally:
            om.sys.stdin = real

    def test_会話記録のパスから見張る相手を組み立てる(self):
        got = om.session_from_file(os.path.join("proj", "S-7.jsonl"))
        self.assertEqual(got["id"], "S-7")
        self.assertEqual(got["subagents"],
                         os.path.join("proj", "S-7", "subagents"))

        # 会話記録ではないものを渡されたら、名指しとして扱わない
        self.assertIsNone(om.session_from_file(""))
        self.assertIsNone(om.session_from_file("proj/notes.md"))

    def test_記録がまだ無ければ次の巡回で改めて探す(self):
        """立ち上がりで記録が見つからなくても、そこで諦めない。"""
        empty = tempfile.mkdtemp(prefix="org-monitor-late-")
        try:
            watcher = om.Watcher(self.fx.root, empty)
            self.assertEqual(watcher.state()["session"], "")

            # 記録の置き場が、あとから現れた
            shutil.copytree(self.fx.transcripts, os.path.join(empty, "projects"))
            watcher.transcripts = os.path.join(empty, "projects")
            self.assertEqual(watcher.state()["session"], "S-1")
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    # ---------------------------------------------------------------- 4

    def test_タスク台帳を読んで動いているものを上に並べる(self):
        self.fx.ledger([
            ["T-001", "2026-08-01", "2026-08-02", "設計する", "完了", "高", "org-design", "無し", "無し"],
            ["T-002", "2026-08-01", "2026-08-03", "実装する", "実装中", "高", "org-implementation", "T-001（ブロッカー）", "無し"],
            ["T-003", "2026-08-01", "2026-08-01", "検証する", "未着手", "中", "未割当", "T-002（ブロッカー）", "無し"],
        ])
        tasks = self.fx.watcher().state()["tasks"]

        self.assertEqual([t["id"] for t in tasks], ["T-002", "T-003", "T-001"])
        self.assertEqual(tasks[0]["state"], "実装中")
        self.assertEqual(tasks[0]["owner"], "org-implementation")

    # ---------------------------------------------------------------- 5

    def test_台帳が無くても落ちずに理由を伝える(self):
        state = self.fx.watcher().state()
        self.assertEqual(state["tasks"], [])
        self.assertTrue(any("タスク台帳" in note for note in state["notes"]))

    def test_台帳が壊れていても落ちずに理由を伝える(self):
        docs = os.path.join(self.fx.root, "docs")
        os.makedirs(docs, exist_ok=True)
        with open(os.path.join(docs, "task-list-repo.csv"), "w", encoding="utf-8") as f:
            f.write("ID,タスク名\nT-001,列が足りない\n")

        state = self.fx.watcher().state()
        self.assertEqual(state["tasks"], [])
        self.assertTrue(state["notes"])

    def test_会話記録が無いリポジトリでも落ちない(self):
        empty = tempfile.mkdtemp(prefix="org-monitor-empty-")
        try:
            state = om.Watcher(empty, self.fx.transcripts).state()
            self.assertEqual(state["session"], "")
            self.assertEqual(state["agents"], [])
            self.assertTrue(any("会話記録" in note for note in state["notes"]))
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_画面へ渡す形が壊れていない(self):
        """JSON にできない値が混じっていないこと。画面はこれしか読まない。"""
        self.fx.agent("agent-aaa", "toolu_A", "org-implementation",
                      "実装", "T-007 の実装", [usage_entry()])
        self.fx.ledger([["T-007", "2026-08-01", "2026-08-02", "実装する",
                         "実装中", "高", "org-implementation", "無し", "無し"]])

        state = self.fx.watcher().state()
        json.dumps(state, ensure_ascii=False)     # 例外が出なければよい

        for key in ["repo", "root", "session", "generated", "refresh",
                    "agents", "running", "tasks", "tokens", "notes"]:
            self.assertIn(key, state)


if __name__ == "__main__":
    unittest.main(verbosity=2)
