#!/usr/bin/env python3
"""org/scripts/org-monitor.py の検証。

実行:
    python3 tests/test_org_monitor.py        （`python3` が無ければ `python`）

標準ライブラリの unittest だけを使う。検証対象のスクリプトが「追加インストール
を要求しない」という制約で書かれているため、その検証も同じ条件で走れないと、
配布先で確かめられない。

ここで確かめるのは、机上で正しさを議論しにくい5点である。

  1. 担当エージェントが「まだ動いているか」の判定。起動を識別する番号
     （toolUseId）が、メインセッションの会話記録に実行結果として現れたか
     どうかで決まる——この判定が、画面の中心になる
  2. 会話記録を「前回の続きから」読むこと。2秒ごとに全部読み直すと数メガ
     バイトの読み込みが繰り返され、更新に耐えない。かつ二重計上してはいけない
  3. 書き込みの途中の行（改行がまだ来ていない行）で壊れないこと。会話記録は
     セッションが動いている最中に読まれるので、これは正常な出来事である
  4. タスク台帳の読み取りと並び順。動いているタスクが上に来ること
  5. 台帳や会話記録が無い・壊れている場合に、落ちずに理由を伝えること

画面（HTML）と待ち受け（HTTPサーバ）は検証しない。状態の組み立てさえ正しけ
れば、そこは表示するだけの層である。
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
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

    def test_稼働中と終了済みを分ける(self):
        """実行結果の行が現れた体だけが「終了」になる。"""
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

        self.assertFalse(by_task["T-007"]["running"], "実行結果が来た体は終了")
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
