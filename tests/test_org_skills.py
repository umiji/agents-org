#!/usr/bin/env python3
"""org/scripts/org-skills.py の検証。

実行:
    python3 tests/test_org_skills.py        （`python3` が無ければ `python`）

標準ライブラリの unittest だけを使う。検証対象のスクリプト本体が「追加インストール
を要求しない」という制約で書かれているため、その検証も同じ条件で走れないと、
配布先で確かめられない。

ここで確かめるのは、机上で正しさを議論しにくい点である。

  1. 収集の網羅と、呼び出し名の組み立て（`プラグイン名:スキル名`）
  2. **特定のリポジトリでだけ有効なプラグインの扱い。** 対象リポジトリが違うのに
     一覧へ出すと「在るはずなのに呼べない」という取り違えが起きる
  3. `SKILL.md` の冒頭の読み取り（折り返し、YAML のブロック記法、引用符）
  4. スキルではないディレクトリを数えないこと
  5. **他人が書いた説明文に、この実行環境の文字コードで表現できない文字が
     混ざっても落ちないこと。** 実際に落ちた
  6. 配布元の目録で、導入済みと未導入を取り違えないこと

**本物の実行環境（`~/.claude`）は読まない。** 使い捨てのディレクトリに偽の環境を
組み立てて試す。本物を読むと、検証結果がそのマシンの導入状況に左右される。
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "..", "org", "scripts", "org-skills.py")

# ファイル名にハイフンが入っていて `import` できないので、パスから直接読み込む。
_spec = importlib.util.spec_from_file_location("org_skills", TARGET)
osk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(osk)


class Base(unittest.TestCase):
    """偽の設定ディレクトリと偽の対象リポジトリを1組作り、そこで試す。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="org-skills-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "claude-home")
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.home, "plugins"))
        os.makedirs(self.root)

    # --- 偽の環境を組み立てる ---

    def write(self, path, body):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    def skill_md(self, description="説明", name=None, body="\n# 見出し\n"):
        name_line = "name: {}\n".format(name) if name else ""
        return "---\n{}description: {}\n---\n{}".format(name_line, description, body)

    def plugin_skill(self, marketplace, plugin, skill, version="1.0.0", **kw):
        """導入済みプラグインの中へスキルを1本置く。"""
        path = os.path.join(
            self.home, "plugins", "cache", marketplace, plugin, version,
            "skills", skill, "SKILL.md")
        return self.write(path, self.skill_md(**kw))

    def user_skill(self, skill, **kw):
        """利用者共通のスキルを1本置く。"""
        return self.write(
            os.path.join(self.home, "skills", skill, "SKILL.md"),
            self.skill_md(**kw))

    def repo_skill(self, skill, **kw):
        """対象リポジトリの中のスキルを1本置く。"""
        return self.write(
            os.path.join(self.root, ".claude", "skills", skill, "SKILL.md"),
            self.skill_md(**kw))

    def install(self, records):
        """導入済みプラグインの記録を書く。records は {"名前@配布元": [記録, ...]}。"""
        self.write(
            os.path.join(self.home, "plugins", "installed_plugins.json"),
            json.dumps({"version": 2, "plugins": records}, ensure_ascii=False))

    def simple_install(self, marketplace, plugin, version="1.0.0", scope="user",
                       project=None):
        record = {"scope": scope, "version": version}
        if project:
            record["projectPath"] = project
        self.install({"{}@{}".format(plugin, marketplace): [record]})

    def marketplace(self, name, plugins):
        """配布元の目録を置き、登録済みにする。"""
        self.write(
            os.path.join(self.home, "plugins", "marketplaces", name,
                         ".claude-plugin", "marketplace.json"),
            json.dumps({"name": name, "plugins": plugins}, ensure_ascii=False))
        known_path = os.path.join(self.home, "plugins", "known_marketplaces.json")
        known = {}
        if os.path.exists(known_path):
            with open(known_path, encoding="utf-8") as f:
                known = json.load(f)
        known[name] = {"installLocation": "/存在しない/場所"}
        self.write(known_path, json.dumps(known, ensure_ascii=False))

    # --- 動かす ---

    def collect(self):
        return osk.collect(self.home, self.root)

    def names(self):
        return [s["name"] for s in self.collect()]

    def run_cli(self, *args):
        """コマンドとして動かし、終了コードと画面出力を返す。"""
        buf = io.StringIO()
        argv = ["--root", self.root, "--claude-home", self.home] + list(args)
        with contextlib.redirect_stdout(buf):
            code = osk.main(argv)
        return code, buf.getvalue()


class 収集(Base):

    def test_プラグインのスキルはプラグイン名を頭に付けて呼ぶ(self):
        self.simple_install("市場", "ecc")
        self.plugin_skill("市場", "ecc", "security-review")
        self.assertEqual(self.names(), ["ecc:security-review"])

    def test_3か所すべてから集める(self):
        self.simple_install("市場", "ecc")
        self.plugin_skill("市場", "ecc", "a")
        self.user_skill("b")
        self.repo_skill("c")
        self.assertEqual(sorted(self.names()), ["b", "c", "ecc:a"])

    def test_出所が記録される(self):
        self.simple_install("市場", "ecc")
        self.plugin_skill("市場", "ecc", "a")
        self.user_skill("b")
        self.repo_skill("c")
        origins = {s["name"]: s["origin"] for s in self.collect()}
        self.assertEqual(origins["ecc:a"], "プラグイン")
        self.assertEqual(origins["b"], "利用者")
        self.assertEqual(origins["c"], "リポジトリ")

    def test_プラグインが1つも無くても落ちない(self):
        self.assertEqual(self.collect(), [])

    def test_設定ディレクトリが無ければ終了コード2(self):
        shutil.rmtree(self.home)
        code, out = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("設定ディレクトリ", out)

    def test_同じ名前が2か所にあっても1本にする(self):
        self.user_skill("同じ名前")
        self.repo_skill("同じ名前")
        self.assertEqual(self.names(), ["同じ名前"])


class リポジトリ限定のプラグイン(Base):
    """特定のリポジトリでだけ有効にしたプラグインの扱い。取り違えの元になる。"""

    def test_対象リポジトリが一致すれば数える(self):
        self.simple_install("市場", "限定", scope="project", project=self.root)
        self.plugin_skill("市場", "限定", "a")
        self.assertEqual(self.names(), ["限定:a"])

    def test_別のリポジトリ向けなら数えない(self):
        self.simple_install("市場", "限定", scope="project",
                            project=os.path.join(self.tmp, "別のリポジトリ"))
        self.plugin_skill("市場", "限定", "a")
        self.assertEqual(self.names(), [])

    def test_リポジトリの指定が無い記録は数えない(self):
        self.install({"限定@市場": [{"scope": "project", "version": "1.0.0"}]})
        self.plugin_skill("市場", "限定", "a")
        self.assertEqual(self.names(), [])

    def test_利用者全体への導入と重ねて記録されていても1本にする(self):
        self.install({"両方@市場": [
            {"scope": "project", "version": "1.0.0", "projectPath": self.root},
            {"scope": "user", "version": "1.0.0"},
        ]})
        self.plugin_skill("市場", "両方", "a")
        self.assertEqual(self.names(), ["両方:a"])


class 冒頭の読み取り(Base):
    """`SKILL.md` の冒頭（フロントマター）から名前と説明を取り出す部分。"""

    def description_of(self, raw):
        self.write(
            os.path.join(self.root, ".claude", "skills", "s", "SKILL.md"), raw)
        return self.collect()[0]["description"]

    def test_折り返した説明が途中で切れない(self):
        raw = ("---\nname: s\ndescription: 前半の文であり、\n"
               "  後半へ続く。\n---\n本文\n")
        self.assertEqual(self.description_of(raw), "前半の文であり、 後半へ続く。")

    def test_ブロック記法の記号を説明に混ぜない(self):
        raw = "---\nname: s\ndescription: >-\n  実際の中身。\n---\n本文\n"
        self.assertEqual(self.description_of(raw), "実際の中身。")

    def test_引用符を外す(self):
        raw = '---\nname: s\ndescription: "囲われた説明"\n---\n本文\n'
        self.assertEqual(self.description_of(raw), "囲われた説明")

    def test_説明が無くても落ちない(self):
        raw = "---\nname: s\n---\n本文\n"
        self.assertEqual(self.description_of(raw), "")

    def test_冒頭が無くても落ちない(self):
        raw = "# ただの見出し\n\n本文\n"
        self.assertEqual(self.description_of(raw), "")

    def test_呼び出し名はディレクトリ名を採る(self):
        """冒頭の `name` と食い違う SKILL.md が実在する。呼ぶときに使うのは前者。"""
        self.repo_skill("ディレクトリ名", name="食い違う名前")
        self.assertEqual(self.names(), ["ディレクトリ名"])


class スキルでないもの(Base):

    def test_SKILL_mdが無いディレクトリは数えない(self):
        os.makedirs(os.path.join(self.home, "skills", "learned"))
        self.user_skill("本物")
        self.assertEqual(self.names(), ["本物"])

    def test_スキルの置き場が無いプラグインを数えない(self):
        """コマンドだけを持ち込むプラグインが実在する。落ちずに素通りすること。"""
        self.simple_install("市場", "コマンドだけ")
        self.write(
            os.path.join(self.home, "plugins", "cache", "市場", "コマンドだけ",
                         "1.0.0", "commands", "何か.md"), "内容")
        self.assertEqual(self.names(), [])

    def test_実体が展開されていない記録を数えない(self):
        self.simple_install("市場", "記録だけ")
        self.assertEqual(self.names(), [])


class 見込みトークン量(Base):

    def test_文字数から概算する(self):
        self.repo_skill("s", body="あ" * 400)
        skill = self.collect()[0]
        self.assertEqual(skill["tokens"], round(skill["chars"] / 4))

    def test_合計を表示する(self):
        self.repo_skill("s")
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("トークン", out)


class 絞り込みと出力(Base):

    def seed(self):
        self.simple_install("市場", "ecc")
        self.plugin_skill("市場", "ecc", "security-review", description="脆弱性を見る")
        self.plugin_skill("市場", "ecc", "api-design", description="APIの設計指針")
        self.repo_skill("独自", description="このリポジトリのもの")

    def test_名前で絞り込む(self):
        self.seed()
        code, out = self.run_cli("--search", "security")
        self.assertEqual(code, 0)
        self.assertIn("ecc:security-review", out)
        self.assertNotIn("ecc:api-design", out)

    def test_説明で絞り込む(self):
        self.seed()
        code, out = self.run_cli("--search", "設計指針")
        self.assertEqual(code, 0)
        self.assertIn("ecc:api-design", out)
        self.assertNotIn("ecc:security-review", out)

    def test_名前だけの表示に説明を混ぜない(self):
        self.seed()
        code, out = self.run_cli("--names")
        self.assertEqual(code, 0)
        self.assertIn("ecc:api-design", out)
        self.assertNotIn("APIの設計指針", out)

    def test_機械可読の形で出す(self):
        self.seed()
        code, out = self.run_cli("--json")
        self.assertEqual(code, 0)
        items = json.loads(out)
        self.assertEqual(len(items), 3)
        self.assertIn("tokens", items[0])

    def test_長い説明を既定では切り詰める(self):
        self.repo_skill("s", description="あ" * 300)
        code, out = self.run_cli()
        self.assertNotIn("あ" * 300, out)
        code, out = self.run_cli("--full")
        self.assertIn("あ" * 300, out)

    def test_1本も無いときに理由を書く(self):
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("見つからない", out)


class 配布元の目録(Base):

    def seed(self):
        self.simple_install("市場", "導入済プラグイン")
        self.plugin_skill("市場", "導入済プラグイン", "a")
        self.marketplace("市場", [
            {"name": "導入済プラグイン", "description": "もう入っている"},
            {"name": "未導入プラグイン", "description": "まだ入っていない"},
        ])

    def test_導入済みと未導入を区別する(self):
        self.seed()
        catalog = {p["name"]: p["installed"]
                   for p in osk.collect_catalog(self.home, self.root)}
        self.assertTrue(catalog["導入済プラグイン"])
        self.assertFalse(catalog["未導入プラグイン"])

    def test_目録の場所は設定ディレクトリから組み立てる(self):
        """記録された絶対パスは、設定ディレクトリを写した環境では失効している。"""
        self.seed()
        names = [p["name"] for p in osk.collect_catalog(self.home, self.root)]
        self.assertIn("未導入プラグイン", names)

    def test_承認が要ることを画面に出す(self):
        self.seed()
        code, out = self.run_cli("--catalog")
        self.assertEqual(code, 0)
        self.assertIn("PO の承認", out)

    def test_登録済み配布元が無くても落ちない(self):
        code, out = self.run_cli("--catalog")
        self.assertEqual(code, 0)


class 文字コード(Base):
    """他人が書いた説明文には、こちらで表現できない文字が混ざる。実際に落ちた。"""

    def test_表現できない文字が混ざっても落ちない(self):
        # 長いダッシュ（—）は日本語 Windows の既定の文字コード cp932 に無い。
        self.repo_skill("s", description="前half — 後half →")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "cp932"
        result = subprocess.run(
            [sys.executable, os.path.abspath(TARGET),
             "--root", self.root, "--claude-home", self.home],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.assertEqual(result.returncode, 0,
                         msg=result.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
