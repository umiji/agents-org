#!/usr/bin/env python3
"""org/scripts/org-docs.py の検証。

実行:
    python3 tests/test_org_docs.py        （`python3` が無ければ `python`）

標準ライブラリの unittest だけを使う。生成対象のスクリプト本体が
「追加インストールを要求しない」という制約で書かれているため、その検証も
同じ条件で走れないと、配布先で確かめられない。

ここで確かめるのは、机上で正しさを議論しにくい6点である。

  1. マスタが無いリポジトリで何もしないこと（組織を動かしていない普通の
     リポジトリでファイルを作らない）
  2. 結合の順序と見出しの段下げが、書いた通りに決まること
  3. コードブロックの中の `#` を見出しと取り違えないこと
  4. マスタ同士のリンクが、結合後の文書内で迷子にならないこと
  5. README の手書き部分を壊さないこと
  6. 何度実行しても結果が変わらないこと（生成物なので捨てて作り直せる、
     という前提がここで成立する）
"""

import contextlib
import importlib.util
import io
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "..", "org", "scripts", "org-docs.py")

# ファイル名にハイフンが入っていて `import` できないので、パスから直接読み込む。
_spec = importlib.util.spec_from_file_location("org_docs", TARGET)
od = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(od)


class Base(unittest.TestCase):
    """使い捨てのリポジトリを1つ作り、そこへマスタを置いて試す。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="org-docs-test-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    # --- 準備 ---

    def master(self, relpath, body):
        """マスタを1本置く。relpath は docs/ から見た相対パス。"""
        return self.write("docs/" + relpath, body)

    def write(self, relpath, body):
        path = os.path.join(self.root, *relpath.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    # --- 実行 ---

    def run_cli(self, *args):
        """スクリプトを呼び、終了コードと標準出力を返す。"""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = od.main(["--root", self.root, *args])
        return code, buf.getvalue()

    # --- 確認 ---

    def read(self, relpath):
        path = os.path.join(self.root, *relpath.split("/"))
        with open(path, encoding="utf-8") as f:
            return f.read()

    def exists(self, relpath):
        return os.path.exists(os.path.join(self.root, *relpath.split("/")))


class マスタが無いとき(Base):

    def test_何も作らずに終了コード0で終わる(self):
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertFalse(self.exists("docs/handbook.md"))
        self.assertIn("マスタ", out)

    def test_READMEがあっても触らない(self):
        self.write("README.md", "# 手書き\n")
        code, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(self.read("README.md"), "# 手書き\n")


class 結合(Base):

    def seed(self):
        self.master("features/login.md", "# ログイン\n\n本文A\n")
        self.master("features/alpha.md", "# 先頭に来る機能\n\n本文B\n")
        self.master("guides/getting-started.md", "# はじめかた\n\n本文C\n")
        self.master("api/rest.md", "# REST API\n\n本文D\n")

    def test_機能ガイドAPIの順に並ぶ(self):
        self.seed()
        self.run_cli()
        text = self.read("docs/handbook.md")
        body = text[text.index("## 先頭に来る機能"):]
        order = [body.index(t) for t in ("先頭に来る機能", "ログイン", "はじめかた", "REST API")]
        self.assertEqual(order, sorted(order))

    def test_同じ分類の中はファイル名の順に並ぶ(self):
        self.seed()
        self.run_cli()
        text = self.read("docs/handbook.md")
        body = text[text.index("## 先頭に来る機能"):]
        # alpha.md < login.md なので「先頭に来る機能」が先。
        self.assertLess(body.index("先頭に来る機能"), body.index("ログイン"))

    def test_見出しが1段下がる(self):
        self.master("features/x.md", "# 題\n\n## 節\n\n### 小節\n")
        self.run_cli()
        text = self.read("docs/handbook.md")
        self.assertIn("\n## 題\n", text)
        self.assertIn("\n### 節\n", text)
        self.assertIn("\n#### 小節\n", text)

    def test_コードブロックの中のシャープは見出しにしない(self):
        self.master("features/x.md", "# 題\n\n```sh\n# これはコメント\n```\n")
        self.run_cli()
        text = self.read("docs/handbook.md")
        self.assertIn("\n# これはコメント\n", text)

    def test_見出しの無いマスタはファイル名を題にする(self):
        self.master("features/no-title.md", "題を書き忘れた本文\n")
        self.run_cli()
        text = self.read("docs/handbook.md")
        self.assertIn("## no-title", text)

    def test_生成物には手で書くなという注意書きが入る(self):
        self.master("features/x.md", "# 題\n")
        self.run_cli()
        self.assertIn("手で書かない", self.read("docs/handbook.md"))

    def test_目次の行き先が本文の見出しと一致する(self):
        self.master("features/x.md", "# ログイン機能\n\n本文\n")
        self.run_cli()
        text = self.read("docs/handbook.md")
        self.assertIn("(#ログイン機能)", text)

    def test_題が同じマスタでも行き先が衝突しない(self):
        self.master("features/a.md", "# 同じ題\n\n本文A\n")
        self.master("guides/b.md", "# 同じ題\n\n本文B\n")
        self.run_cli()
        text = self.read("docs/handbook.md")
        self.assertIn("(#同じ題)", text)
        self.assertIn("(#同じ題-1)", text)


class マスタ同士のリンク(Base):

    def test_他のマスタへのリンクが文書内の行き先に変わる(self):
        self.master("features/a.md", "# 機能A\n\n[機能B](b.md) を見よ\n")
        self.master("features/b.md", "# 機能B\n\n本文\n")
        self.run_cli()
        text = self.read("docs/handbook.md")
        # 行き先は英字を小文字にした形になる（Markdown の表示側と同じ規則）。
        self.assertIn("[機能B](#機能b)", text)

    def test_分類をまたぐ相対リンクも書き換わる(self):
        self.master("guides/g.md", "# ガイド\n\n[機能A](../features/a.md) を見よ\n")
        self.master("features/a.md", "# 機能A\n\n本文\n")
        self.run_cli()
        text = self.read("docs/handbook.md")
        self.assertIn("[機能A](#機能a)", text)

    def test_マスタ以外へのリンクはそのまま残す(self):
        self.master("features/a.md", "# 機能A\n\n[外部](https://example.com) と [雑記](../notes.md)\n")
        self.run_cli()
        text = self.read("docs/handbook.md")
        self.assertIn("(https://example.com)", text)
        self.assertIn("(../notes.md)", text)


class READMEの索引(Base):

    MARKED = (
        "# 製品名\n\n手で書いた前書き。\n\n"
        "<!-- org:docs:begin -->\n"
        "古い索引\n"
        "<!-- org:docs:end -->\n\n"
        "手で書いた後書き。\n"
    )

    def test_目印の区間だけが置き換わる(self):
        self.write("README.md", self.MARKED)
        self.master("features/a.md", "# 機能A\n\n一行目の説明。\n")
        code, _ = self.run_cli()
        self.assertEqual(code, 0)
        text = self.read("README.md")
        self.assertIn("手で書いた前書き。", text)
        self.assertIn("手で書いた後書き。", text)
        self.assertNotIn("古い索引", text)
        self.assertIn("docs/features/a.md", text)

    def test_一行目の説明が索引に載る(self):
        self.write("README.md", self.MARKED)
        self.master("features/a.md", "# 機能A\n\n一行目の説明。\n\n続きの段落。\n")
        self.run_cli()
        text = self.read("README.md")
        self.assertIn("一行目の説明。", text)
        self.assertNotIn("続きの段落。", text)

    def test_目印が無ければREADMEを触らず報告する(self):
        self.write("README.md", "# 目印なし\n")
        self.master("features/a.md", "# 機能A\n")
        code, out = self.run_cli()
        self.assertEqual(self.read("README.md"), "# 目印なし\n")
        self.assertIn("org:docs:begin", out)
        self.assertEqual(code, 0)

    def test_READMEが無ければ作らない(self):
        self.master("features/a.md", "# 機能A\n")
        code, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertFalse(self.exists("README.md"))


class 冪等性と検査(Base):

    def seed(self):
        self.write("README.md",
                   "# 製品名\n\n<!-- org:docs:begin -->\n<!-- org:docs:end -->\n")
        self.master("features/a.md", "# 機能A\n\n説明。\n")
        self.master("guides/g.md", "# ガイド\n\n説明。\n")

    def test_2回実行しても結果が変わらない(self):
        self.seed()
        self.run_cli()
        first = (self.read("docs/handbook.md"), self.read("README.md"))
        self.run_cli()
        second = (self.read("docs/handbook.md"), self.read("README.md"))
        self.assertEqual(first, second)

    def test_検査だけの実行は書き込まない(self):
        self.seed()
        code, out = self.run_cli("--check")
        self.assertEqual(code, 1)
        self.assertFalse(self.exists("docs/handbook.md"))
        self.assertIn("古い", out)

    def test_生成済みなら検査は終了コード0(self):
        self.seed()
        self.run_cli()
        code, _ = self.run_cli("--check")
        self.assertEqual(code, 0)

    def test_マスタを直すと検査が古いと言う(self):
        self.seed()
        self.run_cli()
        self.master("features/a.md", "# 機能A\n\n説明を書き換えた。\n")
        code, _ = self.run_cli("--check")
        self.assertEqual(code, 1)

    def test_マスタを消すと生成物からも消える(self):
        self.seed()
        self.run_cli()
        os.remove(os.path.join(self.root, "docs", "guides", "g.md"))
        self.run_cli()
        self.assertNotIn("ガイド", self.read("docs/handbook.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
