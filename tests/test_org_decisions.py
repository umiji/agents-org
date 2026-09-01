#!/usr/bin/env python3
"""決定ログ索引の生成スクリプト（org/scripts/org-decisions.py）の回帰テスト。

標準ライブラリの unittest だけで動く。配布物が「追加インストールを要求しない」
という制約で書かれている以上、その検証も同じ条件で走れなければ、配布先で
確かめられないためである。

    python3 tests/test_org_decisions.py
"""

from __future__ import annotations

import csv
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, os.pardir, "org", "scripts", "org-decisions.py")


def load_module():
    """ハイフンを含むファイル名なので、通常の import では読み込めない。"""
    spec = importlib.util.spec_from_file_location("org_decisions", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


od = load_module()


# --------------------------------------------------------------------------
# 土台
# --------------------------------------------------------------------------

TEMPLATE_COMMENT = """<!-- 書く条件は4つ: PO の判断を仰いだ / 別の選択肢が実際にあった技術選定 /
     登録後にスコープ・完了条件・禁止事項が変わった / 理由を知らない担当が
     将来これを元に戻してしまいそうなもの

### YYYY-MM-DD 一行でわかる決定の要約
- 対象: 決定が支配する領域や部品を1〜3語で
- 決定: 実際に決めたこと
-->"""


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="org-decisions-test-")
        os.makedirs(os.path.join(self.root, "docs", "tasks"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def task(self, task_id: str, decision_log: str, title: str = "テスト用タスク"):
        """タスク別ファイルを1本置く。決定ログ以外は最小限。"""
        body = (
            "# {tid} {title}\n\n"
            "## メタデータ\n"
            "- 状態: 完了\n\n"
            "## 目的\n"
            "テスト用。\n\n"
            "## 決定ログ\n"
            "{log}\n\n"
            "## 作業ログ\n"
            "特記なし\n\n"
            "## 証拠\n"
            "特記なし\n"
        ).format(tid=task_id, title=title, log=decision_log)
        path = os.path.join(self.root, "docs", "tasks", "{}.md".format(task_id))
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        return path

    def run_script(self, *argv):
        """スクリプトを走らせ、(終了コード, 標準出力) を返す。"""
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = od.main(["--root", self.root] + list(argv))
        return code, buf.getvalue()

    @property
    def index_path(self):
        return os.path.join(self.root, od.INDEX)

    def read_index(self):
        """索引を読み、(先頭の注意書き, 行の一覧) を返す。"""
        with open(self.index_path, encoding="utf-8", newline="") as f:
            text = f.read()
        first, _, rest = text.partition("\n")
        rows = list(csv.DictReader(io.StringIO(rest)))
        return first, rows


# --------------------------------------------------------------------------
# 何も無いとき
# --------------------------------------------------------------------------

class TestEmpty(Base):
    def test_タスク別ファイルが1本も無ければ何も書かない(self):
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.index_path))
        self.assertIn("タスク別ファイル", out)

    def test_タスクの置き場そのものが無くても落ちない(self):
        shutil.rmtree(os.path.join(self.root, "docs", "tasks"))
        code, _ = self.run_script()
        self.assertEqual(code, 0)

    def test_決定が1件も無ければ索引を作らない(self):
        self.task("T-001", "特記なし")
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.index_path))
        self.assertIn("決定", out)

    def test_雛形の案内文だけの決定ログは決定として数えない(self):
        self.task("T-001", "特記なし\n" + TEMPLATE_COMMENT)
        code, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.index_path))

    def test_記録なしは決定として数えない(self):
        self.task("T-001", "（記録なし）")
        code, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.index_path))


# --------------------------------------------------------------------------
# 基本の読み取り
# --------------------------------------------------------------------------

class TestParse(Base):
    def test_決定を1件読んで索引の1行にする(self):
        self.task("T-007", """### 2026-05-10 認証ライブラリは A を使う
- 対象: 認証
- 決定: 認証ライブラリに A を採用する
- 却下案: B → 依存が重い
- 出典: abc1234
""")
        code, _ = self.run_script()
        self.assertEqual(code, 0)
        banner, rows = self.read_index()
        self.assertIn("手で編集しない", banner)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["対象"], "認証")
        self.assertEqual(row["日付"], "2026-05-10")
        self.assertEqual(row["要約"], "認証ライブラリは A を使う")
        self.assertEqual(row["決定"], "認証ライブラリに A を採用する")
        self.assertEqual(row["タスク"], "T-007")
        self.assertEqual(row["状態"], od.ACTIVE)

    def test_列の並びは決めたとおり(self):
        self.task("T-001", "### 2026-01-01 何かを決めた\n- 対象: X\n- 決定: Y\n")
        self.run_script()
        with open(self.index_path, encoding="utf-8", newline="") as f:
            f.readline()  # 注意書き
            header = f.readline().strip()
        self.assertEqual(header, ",".join(od.COLUMNS))

    def test_1つのタスクが決定を複数持てる(self):
        self.task("T-007", """### 2026-05-10 古いほう
- 対象: 認証
- 決定: A にする

### 2026-06-20 新しいほう
- 対象: 認証
- 決定: B にする
""")
        self.run_script()
        _, rows = self.read_index()
        self.assertEqual(len(rows), 2)
        # 同じ対象の中は日付の新しい順
        self.assertEqual([r["日付"] for r in rows], ["2026-06-20", "2026-05-10"])

    def test_全角コロンでも読める(self):
        self.task("T-001", "### 2026-01-01 決めた\n- 対象：認証\n- 決定：A にする\n")
        self.run_script()
        _, rows = self.read_index()
        self.assertEqual(rows[0]["対象"], "認証")
        self.assertEqual(rows[0]["決定"], "A にする")

    def test_決定ログの外の見出しは拾わない(self):
        self.task("T-001", "### 2026-01-01 決めた\n- 対象: 認証\n- 決定: A\n")
        path = os.path.join(self.root, "docs", "tasks", "T-001.md")
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n## 作業ログ2\n### 2026-02-02 これは決定ではない\n- 対象: 罠\n")
        self.run_script()
        _, rows = self.read_index()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["対象"], "認証")

    def test_完了や中止のタスクも索引に入る(self):
        # 「完了済みは読まない」という規約が、決定が引けない原因だった。
        p = self.task("T-001", "### 2026-01-01 決めた\n- 対象: 認証\n- 決定: A\n")
        with open(p, encoding="utf-8") as f:
            text = f.read()
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text.replace("- 状態: 完了", "- 状態: 中止"))
        self.run_script()
        _, rows = self.read_index()
        self.assertEqual(len(rows), 1)


# --------------------------------------------------------------------------
# 並び順
# --------------------------------------------------------------------------

class TestOrder(Base):
    def test_対象ごとに束ね対象は名前順その中は日付の新しい順(self):
        self.task("T-001", """### 2026-01-01 古い認証の決定
- 対象: 認証
- 決定: A

### 2026-03-03 新しい認証の決定
- 対象: 認証
- 決定: B
""")
        self.task("T-002", """### 2026-02-02 配信の決定
- 対象: 配信
- 決定: C
""")
        self.run_script()
        _, rows = self.read_index()
        self.assertEqual([(r["対象"], r["日付"]) for r in rows],
                         [("認証", "2026-03-03"), ("認証", "2026-01-01"), ("配信", "2026-02-02")])

    def test_同じ日付ならタスク番号の数の順(self):
        self.task("T-002", "### 2026-01-01 二番\n- 対象: 認証\n- 決定: B\n")
        self.task("T-010", "### 2026-01-01 十番\n- 対象: 認証\n- 決定: J\n")
        self.run_script()
        _, rows = self.read_index()
        self.assertEqual([r["タスク"] for r in rows], ["T-002", "T-010"])


# --------------------------------------------------------------------------
# 失効（上書き対象）
# --------------------------------------------------------------------------

class TestSupersede(Base):
    def _two(self):
        self.task("T-003", "### 2026-05-10 認証ライブラリは A を使う\n- 対象: 認証\n- 決定: A\n")
        self.task("T-021", """### 2026-08-12 認証ライブラリを B へ移す
- 対象: 認証
- 決定: B へ移す
- 上書き対象: T-003 2026-05-10
""")

    def test_古い側の状態に置き換え済みと置き換え先が出る(self):
        self._two()
        self.run_script()
        _, rows = self.read_index()
        old = [r for r in rows if r["タスク"] == "T-003"][0]
        new = [r for r in rows if r["タスク"] == "T-021"][0]
        self.assertIn("置き換え済み", old["状態"])
        self.assertIn("T-021", old["状態"])
        self.assertEqual(new["状態"], od.ACTIVE)

    def test_過去のタスク別ファイルは一文字も書き換えない(self):
        self._two()
        path = os.path.join(self.root, "docs", "tasks", "T-003.md")
        with open(path, encoding="utf-8") as f:
            before = f.read()
        self.run_script()
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)

    def test_1つの決定が複数を失効させられる(self):
        self.task("T-001", "### 2026-01-01 いち\n- 対象: 認証\n- 決定: A\n")
        self.task("T-002", "### 2026-02-02 に\n- 対象: 認証\n- 決定: B\n")
        self.task("T-003", """### 2026-03-03 まとめて置き換える
- 対象: 認証
- 決定: C
- 上書き対象: T-001 2026-01-01 / T-002 2026-02-02
""")
        self.run_script()
        _, rows = self.read_index()
        states = {r["タスク"]: r["状態"] for r in rows}
        self.assertIn("置き換え済み", states["T-001"])
        self.assertIn("置き換え済み", states["T-002"])
        self.assertEqual(states["T-003"], od.ACTIVE)

    def test_実在しない決定を指していたら警告する(self):
        self.task("T-021", """### 2026-08-12 存在しないものを指す
- 対象: 認証
- 決定: B
- 上書き対象: T-999 2020-01-01
""")
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn("T-999", out)
        self.assertIn("上書き対象", out)

    def test_日付だけ違えば別の決定として扱う(self):
        # 1つのタスクが決定を複数持ちうるため、日付まで含めて指す決まりである。
        self.task("T-003", """### 2026-05-10 ひとつめ
- 対象: 認証
- 決定: A

### 2026-06-10 ふたつめ
- 対象: 認証
- 決定: B
""")
        self.task("T-021", """### 2026-08-12 ひとつめだけ置き換える
- 対象: 認証
- 決定: C
- 上書き対象: T-003 2026-05-10
""")
        self.run_script()
        _, rows = self.read_index()
        by_date = {r["日付"]: r["状態"] for r in rows}
        self.assertIn("置き換え済み", by_date["2026-05-10"])
        self.assertEqual(by_date["2026-06-10"], od.ACTIVE)


# --------------------------------------------------------------------------
# 書式が崩れている入力
# --------------------------------------------------------------------------

class TestMalformed(Base):
    def test_対象が無い決定は未分類へ入れて警告する(self):
        self.task("T-001", "### 2026-01-01 対象を書き忘れた\n- 決定: A\n")
        code, out = self.run_script()
        self.assertEqual(code, 0)
        _, rows = self.read_index()
        self.assertEqual(rows[0]["対象"], od.UNCLASSIFIED)
        self.assertIn("対象", out)
        self.assertIn("T-001", out)

    def test_日付が読めない見出しは警告して飛ばす(self):
        self.task("T-001", "### 決定っぽい見出しだが日付が無い\n- 対象: 認証\n- 決定: A\n")
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.index_path))
        self.assertIn("日付", out)

    def test_決定の行が無ければ要約で埋めて警告する(self):
        self.task("T-001", "### 2026-01-01 決定の行を書き忘れた\n- 対象: 認証\n")
        code, out = self.run_script()
        self.assertEqual(code, 0)
        _, rows = self.read_index()
        self.assertEqual(rows[0]["決定"], "決定の行を書き忘れた")
        self.assertIn("決定", out)

    def test_長い決定は切り詰める(self):
        long_text = "あ" * 300
        self.task("T-001", "### 2026-01-01 長い\n- 対象: 認証\n- 決定: %s\n" % long_text)
        self.run_script()
        _, rows = self.read_index()
        self.assertLessEqual(len(rows[0]["決定"]), od.SUMMARY_LIMIT + 1)
        self.assertTrue(rows[0]["決定"].endswith("…"))

    def test_カンマや引用符を含んでもCSVが壊れない(self):
        self.task("T-001", '### 2026-01-01 A, B, "C" を比べた\n- 対象: 認証, 認可\n- 決定: A, B ではなく "C"\n')
        self.run_script()
        _, rows = self.read_index()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["対象"], "認証, 認可")
        self.assertEqual(rows[0]["決定"], 'A, B ではなく "C"')

    def test_改行を含む値は1行に畳む(self):
        self.task("T-001", "### 2026-01-01 決めた\n- 対象: 認証\n- 決定: 一行目\n  二行目\n")
        self.run_script()
        _, rows = self.read_index()
        self.assertNotIn("\n", rows[0]["決定"])

    def test_タスク別ファイルが読めなくても他は処理する(self):
        self.task("T-001", "### 2026-01-01 決めた\n- 対象: 認証\n- 決定: A\n")
        # ディレクトリを .md という名前で置き、読めないファイルを模す
        os.makedirs(os.path.join(self.root, "docs", "tasks", "T-002.md"))
        code, _ = self.run_script()
        self.assertEqual(code, 0)
        _, rows = self.read_index()
        self.assertEqual(len(rows), 1)


# --------------------------------------------------------------------------
# 生成物としての性質
# --------------------------------------------------------------------------

class TestGenerated(Base):
    def test_何度走らせても結果は同じ(self):
        self.task("T-001", "### 2026-01-01 決めた\n- 対象: 認証\n- 決定: A\n")
        self.run_script()
        with open(self.index_path, encoding="utf-8") as f:
            first = f.read()
        self.run_script()
        with open(self.index_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), first)

    def test_手で書き足した行は次の生成で消える(self):
        self.task("T-001", "### 2026-01-01 決めた\n- 対象: 認証\n- 決定: A\n")
        self.run_script()
        with open(self.index_path, "a", encoding="utf-8", newline="") as f:
            f.write("手書き,2026-01-01,勝手に足した行,,,\n")
        self.run_script()
        _, rows = self.read_index()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("手書き", [r["対象"] for r in rows])

    def test_決定が全部消えたら索引も消す(self):
        p = self.task("T-001", "### 2026-01-01 決めた\n- 対象: 認証\n- 決定: A\n")
        self.run_script()
        self.assertTrue(os.path.exists(self.index_path))
        with open(p, encoding="utf-8") as f:
            text = f.read()
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text.replace("### 2026-01-01 決めた\n- 対象: 認証\n- 決定: A", "特記なし"))
        self.run_script()
        self.assertFalse(os.path.exists(self.index_path))


# --------------------------------------------------------------------------
# --check と --hook
# --------------------------------------------------------------------------

class TestModes(Base):
    def test_checkは最新なら0(self):
        self.task("T-001", "### 2026-01-01 決めた\n- 対象: 認証\n- 決定: A\n")
        self.run_script()
        code, _ = self.run_script("--check")
        self.assertEqual(code, 0)

    def test_checkは古ければ1(self):
        self.task("T-001", "### 2026-01-01 決めた\n- 対象: 認証\n- 決定: A\n")
        code, out = self.run_script("--check")
        self.assertEqual(code, 1)
        self.assertIn("古い", out)

    def test_checkは書き込まない(self):
        self.task("T-001", "### 2026-01-01 決めた\n- 対象: 認証\n- 決定: A\n")
        self.run_script("--check")
        self.assertFalse(os.path.exists(self.index_path))

    def test_hookは書き込んだうえで常に0で終わる(self):
        self.task("T-001", "### 2026-01-01 決めた\n- 対象: 認証\n- 決定: A\n")
        code, _ = self.run_script("--hook")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.index_path))

    def test_hookは対象リポジトリが無くても0で終わる(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = od.main(["--root", os.path.join(self.root, "存在しない"), "--hook"])
        self.assertEqual(code, 0)

    def test_hookの出力は数行に収まる(self):
        self.task("T-001", "### 2026-01-01 決めた\n- 対象: 認証\n- 決定: A\n")
        _, out = self.run_script("--hook")
        self.assertLessEqual(len([l for l in out.splitlines() if l.strip()]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=1)
