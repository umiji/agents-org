#!/usr/bin/env python3
"""org/scripts/org-tokens.py の検証。

実行:
    python3 tests/test_org_tokens.py        （`python3` が無ければ `python`）

標準ライブラリの unittest だけを使う。集計対象のスクリプト本体が
「追加インストールを要求しない」という制約で書かれているため、その検証も
同じ条件で走れないと、配布先で確かめられない。

ここで確かめるのは、机上で正しさを議論しにくい4点である。

  1. 会話記録の置き場を、ディレクトリ名からではなく記録の中身から見つけること
  2. タスクIDの紐付け（オーケストレーターは前方に引きずる／担当エージェントは
     指示文の先頭だけを見る）
  3. 何度更新しても二重計上にならないこと。フックから繰り返し呼ばれる前提のため
  4. 会話記録が消えた過去のセッションの行を、消さずに残すこと
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "..", "org", "scripts", "org-tokens.py")

# ファイル名にハイフンが入っていて `import` できないので、パスから直接読み込む。
_spec = importlib.util.spec_from_file_location("org_tokens", TARGET)
ot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ot)


# --------------------------------------------------------------------------
# 会話記録を組み立てる道具
# --------------------------------------------------------------------------

def assistant(model, cwd, out=0, cache_read=0, cache_write_5m=0, cache_write_1h=0,
              legacy_write=None, text=""):
    """モデルの返答1回分の記録。"""
    usage = {
        "input_tokens": 0,
        "output_tokens": out,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": legacy_write if legacy_write is not None else 0,
    }
    if legacy_write is None:
        usage["cache_creation"] = {
            "ephemeral_5m_input_tokens": cache_write_5m,
            "ephemeral_1h_input_tokens": cache_write_1h,
        }
    return {"type": "assistant", "cwd": cwd,
            "message": {"model": model, "usage": usage,
                        "content": [{"type": "text", "text": text}]}}


def human(cwd, text):
    """人間（またはオーケストレーター）が書いた入力。content は文字列になる。"""
    return {"type": "user", "cwd": cwd, "message": {"content": text}}


def tool_result(cwd, text):
    """道具の実行結果。content は配列になる。ここのタスクIDは拾ってはいけない。"""
    return {"type": "user", "cwd": cwd,
            "message": {"content": [{"type": "tool_result", "content": text}]}}


def write_jsonl(path, entries, broken_tail=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        if broken_tail:
            # 書き込みの途中で読まれた状態。壊れていても止まってはいけない。
            f.write('{"type": "assistant", "message": {"mod')


class Fixture:
    """1つのリポジトリと、それに対応する会話記録の置き場を用意する。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "repo")
        self.transcripts = os.path.join(self.tmp, "projects")
        os.makedirs(os.path.join(self.root, "docs"))
        os.makedirs(self.transcripts)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def session(self, session_id, entries, cwd=None, broken_tail=False, project="proj"):
        base = os.path.join(self.transcripts, project)
        write_jsonl(os.path.join(base, session_id + ".jsonl"), entries, broken_tail)
        return base

    def agent(self, session_id, name, agent_type, entries, project="proj"):
        base = os.path.join(self.transcripts, project, session_id, "subagents")
        write_jsonl(os.path.join(base, name + ".jsonl"), entries)
        with open(os.path.join(base, name + ".meta.json"), "w", encoding="utf-8") as f:
            json.dump({"agentType": agent_type, "spawnDepth": 1}, f)

    def update(self):
        return ot.update(self.root, self.transcripts)

    def rows(self):
        return ot.read_ledger(ot.ledger_path(self.root))


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)

    def totals_by(self, key):
        out = {}
        for r in self.fx.rows():
            out[r[key]] = out.get(r[key], 0) + r["合計"]
        return out


# --------------------------------------------------------------------------

class TestFindSessions(Base):
    def test_記録の中のcwdで突き合わせる(self):
        """ディレクトリ名からは探さない。同じパスでも大文字小文字違いで
        別ディレクトリになっている実例があるため。"""
        self.fx.session("s1", [human(self.fx.root, "hello")], project="名前は関係ない")
        found = ot.find_sessions(self.fx.root, self.fx.transcripts)
        self.assertEqual([s["id"] for s in found], ["s1"])

    def test_別のリポジトリの記録は拾わない(self):
        self.fx.session("s1", [human(self.fx.root, "a")])
        self.fx.session("s2", [human(os.path.join(self.fx.tmp, "よそ"), "b")])
        found = ot.find_sessions(self.fx.root, self.fx.transcripts)
        self.assertEqual([s["id"] for s in found], ["s1"])

    def test_大文字小文字が違っても同じ場所とみなす(self):
        # Windows はパスの大文字小文字を区別しない。会話記録には
        # `C:\...` と `c:\...` の両方が現れる。
        self.fx.session("s1", [human(self.fx.root.upper(), "a")])
        found = ot.find_sessions(self.fx.root, self.fx.transcripts)
        self.assertEqual(len(found), 1)

    def test_置き場が無くても落ちない(self):
        self.assertEqual(ot.find_sessions(self.fx.root, os.path.join(self.fx.tmp, "無")), [])


class TestUsage(Base):
    def test_キャッシュ書込は5分と1時間を合算する(self):
        got = ot.usage_of(assistant("claude-opus-5", "x", cache_write_5m=100, cache_write_1h=300))
        self.assertEqual(got[1]["キャッシュ書込"], 400)

    def test_内訳が無ければ旧来の欄を使う(self):
        got = ot.usage_of(assistant("claude-opus-5", "x", legacy_write=250))
        self.assertEqual(got[1]["キャッシュ書込"], 250)

    def test_返答以外は数えない(self):
        self.assertIsNone(ot.usage_of(human("x", "T-001")))
        self.assertIsNone(ot.usage_of({"type": "assistant", "message": {}}))


class TestTaskAttribution(Base):
    def test_オーケストレーターはタスクIDを前方へ引きずる(self):
        cwd = self.fx.root
        self.fx.session("s1", [
            assistant("claude-opus-5", cwd, out=10),               # ID登場前 → タスク外
            human(cwd, "T-001 をやって"),
            assistant("claude-opus-5", cwd, out=20),               # T-001
            assistant("claude-opus-5", cwd, out=30),               # T-001 のまま
            human(cwd, "次は T-002"),
            assistant("claude-opus-5", cwd, out=40),               # T-002
        ])
        self.fx.update()
        self.assertEqual(self.totals_by("タスクID"),
                         {ot.OUTSIDE_TASK: 10, "T-001": 50, "T-002": 40})

    def test_道具の実行結果に出たタスクIDでは切り替えない(self):
        """読み込んだファイルの中身に他のタスクIDが並んでいても、
        担当タスクが乗り換わってはいけない。"""
        cwd = self.fx.root
        self.fx.session("s1", [
            human(cwd, "T-001 をやって"),
            assistant("claude-opus-5", cwd, out=10),
            tool_result(cwd, "T-042 T-099 と書かれたファイルの中身"),
            assistant("claude-opus-5", cwd, out=20),
        ])
        self.fx.update()
        self.assertEqual(self.totals_by("タスクID"), {"T-001": 30})

    def test_桁数の違うタスクIDを取り違えない(self):
        cwd = self.fx.root
        self.fx.session("s1", [
            human(cwd, "T-7 の次に T-70"),
            assistant("claude-opus-5", cwd, out=10),
        ])
        self.fx.update()
        # 同じ行に2つ出たら、後ろのものを採る（直近の指示が有効）。
        self.assertEqual(self.totals_by("タスクID"), {"T-70": 10})

    def test_担当エージェントは指示文の先頭だけを見る(self):
        cwd = self.fx.root
        self.fx.session("s1", [human(cwd, "start")])
        self.fx.agent("s1", "agent-a", "org-implementation", [
            {"type": "user", "message": {"content": "T-005 を実装せよ"}},
            {"type": "user", "message": {"content": [{"type": "tool_result",
                                                      "content": "T-999 と書かれた別ファイル"}]}},
            assistant("claude-sonnet-5", cwd, out=70),
        ])
        self.fx.update()
        rows = [r for r in self.fx.rows() if r["担当"] == "org-implementation"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["タスクID"], "T-005")

    def test_指示文にタスクIDが無ければタスク不明にする(self):
        cwd = self.fx.root
        self.fx.session("s1", [human(cwd, "start")])
        self.fx.agent("s1", "agent-a", "org-review",
                      [{"type": "user", "message": {"content": "調べてくれ"}},
                       assistant("claude-sonnet-5", cwd, out=5)])
        self.fx.update()
        rows = [r for r in self.fx.rows() if r["担当"] == "org-review"]
        self.assertEqual(rows[0]["タスクID"], ot.UNKNOWN_TASK)


class TestLedger(Base):
    def test_担当別とモデル別に分けて記録する(self):
        cwd = self.fx.root
        self.fx.session("s1", [human(cwd, "T-001"),
                               assistant("claude-opus-5", cwd, out=10),
                               assistant("claude-haiku-4-5", cwd, out=3)])
        self.fx.agent("s1", "agent-a", "org-implementation",
                      [{"type": "user", "message": {"content": "T-001 を実装"}},
                       assistant("claude-sonnet-5", cwd, out=100)])
        self.fx.update()
        self.assertEqual(self.totals_by("モデル"),
                         {"claude-opus-5": 10, "claude-haiku-4-5": 3, "claude-sonnet-5": 100})
        self.assertEqual(self.totals_by("担当"),
                         {ot.ORCHESTRATOR: 13, "org-implementation": 100})

    def test_合計は4費目の足し算(self):
        cwd = self.fx.root
        self.fx.session("s1", [assistant("claude-opus-5", cwd, out=7,
                                         cache_read=1000, cache_write_5m=200)])
        self.fx.update()
        row = self.fx.rows()[0]
        self.assertEqual(row["出力"], 7)
        self.assertEqual(row["キャッシュ読出"], 1000)
        self.assertEqual(row["キャッシュ書込"], 200)
        self.assertEqual(row["合計"], 1207)
        self.assertEqual(row["返答回数"], 1)

    def test_壊れた行があっても止まらない(self):
        cwd = self.fx.root
        self.fx.session("s1", [assistant("claude-opus-5", cwd, out=10)], broken_tail=True)
        self.fx.update()
        self.assertEqual(sum(r["合計"] for r in self.fx.rows()), 10)

    def test_タスク台帳の名前にそろえる(self):
        with open(os.path.join(self.fx.root, "docs", "task-list-myapp.csv"), "w",
                  encoding="utf-8") as f:
            f.write("ID\n")
        self.assertTrue(ot.ledger_path(self.fx.root).endswith("token-usage-myapp.csv"))

    def test_台帳が2つあれば止める(self):
        for name in ("token-usage-a.csv", "token-usage-b.csv"):
            with open(os.path.join(self.fx.root, "docs", name), "w", encoding="utf-8") as f:
                f.write("x\n")
        with self.assertRaises(ot.LedgerError):
            ot.ledger_path(self.fx.root)

    def test_列が足りない台帳は読まずに止める(self):
        path = ot.ledger_path(self.fx.root)
        with open(path, "w", encoding="utf-8") as f:
            f.write("タスクID,合計\nT-001,5\n")
        with self.assertRaises(ot.LedgerError):
            ot.read_ledger(path)


class TestIdempotence(Base):
    """フックから何度も呼ばれる前提なので、ここが崩れると数字が信用できなくなる。"""

    def test_2回更新しても二重計上しない(self):
        cwd = self.fx.root
        self.fx.session("s1", [human(cwd, "T-001"), assistant("claude-opus-5", cwd, out=10)])
        self.fx.agent("s1", "agent-a", "org-test",
                      [{"type": "user", "message": {"content": "T-001 のテスト"}},
                       assistant("claude-sonnet-5", cwd, out=50)])
        self.fx.update()
        first = self.fx.rows()
        self.fx.update()
        self.assertEqual(self.fx.rows(), first)
        self.assertEqual(sum(r["合計"] for r in first), 60)

    def test_会話が伸びた分だけ増える(self):
        cwd = self.fx.root
        self.fx.session("s1", [human(cwd, "T-001"), assistant("claude-opus-5", cwd, out=10)])
        self.fx.update()
        self.fx.session("s1", [human(cwd, "T-001"),
                               assistant("claude-opus-5", cwd, out=10),
                               assistant("claude-opus-5", cwd, out=5)])
        self.fx.update()
        self.assertEqual(self.totals_by("タスクID"), {"T-001": 15})

    def test_紐付けが後から変わっても入れ替わる(self):
        """会話が進んでタスクIDが遅れて登場した場合、前の分の帰属が変わる。
        足し込みだと古い帰属が残ってしまう。"""
        cwd = self.fx.root
        self.fx.session("s1", [assistant("claude-opus-5", cwd, out=10)])
        self.fx.update()
        self.assertEqual(self.totals_by("タスクID"), {ot.OUTSIDE_TASK: 10})

        self.fx.session("s1", [human(cwd, "T-003 の話"),
                               assistant("claude-opus-5", cwd, out=10)])
        self.fx.update()
        self.assertEqual(self.totals_by("タスクID"), {"T-003": 10})

    def test_記録が消えたセッションの行は残す(self):
        cwd = self.fx.root
        self.fx.session("s1", [human(cwd, "T-001"), assistant("claude-opus-5", cwd, out=10)])
        self.fx.update()

        os.remove(os.path.join(self.fx.transcripts, "proj", "s1.jsonl"))
        self.fx.session("s2", [human(cwd, "T-002"), assistant("claude-opus-5", cwd, out=20)])
        self.fx.update()
        self.assertEqual(self.totals_by("タスクID"), {"T-001": 10, "T-002": 20})


class TestCli(Base):
    def run_cli(self, *args):
        from io import StringIO
        buf, saved = StringIO(), sys.stdout
        sys.stdout = buf
        try:
            code = ot.main(["--root", self.fx.root, "--transcripts", self.fx.transcripts]
                           + list(args))
        finally:
            sys.stdout = saved
        return code, buf.getvalue()

    def seed(self):
        cwd = self.fx.root
        self.fx.session("s1", [human(cwd, "T-001"),
                               assistant("claude-opus-5", cwd, out=10, cache_read=900)])
        self.fx.agent("s1", "agent-a", "org-implementation",
                      [{"type": "user", "message": {"content": "T-001 を実装"}},
                       assistant("claude-sonnet-5", cwd, out=50)])

    def test_更新してから集計まで通す(self):
        self.seed()
        code, out = self.run_cli("--update")
        self.assertEqual(code, 0)
        self.assertIn("トークン台帳を更新", out)
        # 既定はタスク別の表。オーケストレーターと担当エージェントの2者が
        # 同じ T-001 に乗るので、担当数は 2 になる。
        self.assertIn("T-001", out)
        self.assertEqual(self.totals_by("担当"),
                         {ot.ORCHESTRATOR: 910, "org-implementation": 50})

    def test_軸を変えられる(self):
        self.seed()
        self.run_cli("--update")
        for by, expect in (("agent", "org-implementation"), ("model", "claude-sonnet-5")):
            code, out = self.run_cli("--by", by)
            self.assertEqual(code, 0)
            self.assertIn(expect, out)

    def test_1タスクの内訳を出せる(self):
        self.seed()
        self.run_cli("--update")
        code, out = self.run_cli("--task", "T-001")
        self.assertEqual(code, 0)
        self.assertIn("担当別", out)
        self.assertIn("モデル別", out)

    def test_知らないタスクを指定したら1を返す(self):
        self.seed()
        self.run_cli("--update")
        code, out = self.run_cli("--task", "T-999")
        self.assertEqual(code, 1)

    def test_台帳が空でもフックからは0で終わる(self):
        """終了コードが 0 のときだけ、出力がセッションの文脈へ入るため。"""
        code, _ = self.run_cli("--hook")
        self.assertEqual(code, 0)
        code, _ = self.run_cli("--update", "--hook")
        self.assertEqual(code, 0)

    def test_壊れた台帳でもフックからは0で終わる(self):
        path = ot.ledger_path(self.fx.root)
        with open(path, "w", encoding="utf-8") as f:
            f.write("タスクID,合計\nT-001,5\n")
        code, out = self.run_cli("--hook")
        self.assertEqual(code, 0)
        self.assertIn("必要な列が無い", out)

    def test_ステータス行は1行(self):
        self.seed()
        self.run_cli("--update")
        code, out = self.run_cli("--statusline")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertTrue(out.startswith("[org]"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
