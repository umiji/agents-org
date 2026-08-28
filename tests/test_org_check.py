#!/usr/bin/env python3
"""org/scripts/org-check.py の検証。

実行:
    python3 tests/test_org_check.py         （`python3` が無ければ `python`）

標準ライブラリの unittest だけを使う。検査対象のスクリプト本体が
「追加インストールを要求しない」という制約で書かれているため、その検証も
同じ条件で走れないと、配布先で確かめられない。

このスクリプトは**セッション開始時に自動で走る唯一の実行可能物**であり、
壊れても誰も気づかない。壊れたときに何が起きるかは症状ごとに違う。

  - 停滞の判定が緩む  → 止まっているタスクが報告されない（**気づけない**）
  - 停滞の判定が過剰  → 止まっていないタスクへ対処が向かう（**誤った対処**）
  - 台帳が読めない    → セッション開始時に結果が届かない

ここで固定するのは、机上で正しさを議論しにくい6点である。

  1. 停滞の判定（check）と滞留日数の集計（summarize）が、同じ除外を通ること
     ——**片方だけが除外を持っていた不具合が実際に起きた**
  2. 指示欄の未記入と、完了なのに証拠が無いことを、機械的に捕まえること
  3. PO確認待ちキューの書式違反を捕まえること
  4. 全部埋まっている台帳では、何も警告しないこと（誤検出が無いこと）
  5. 台帳が読めないとき、フックから呼ばれた場合だけ 0 で終わること
     ——Claude Code は終了コードが 0 のときだけ標準出力を読み込むため
  6. 台帳がまだ無いとき、セッション開始を妨げないこと
"""

import datetime as dt
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "..", "org", "scripts", "org-check.py")

# ファイル名にハイフンが入っていて `import` できないので、パスから直接読み込む。
_spec = importlib.util.spec_from_file_location("org_check", TARGET)
oc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oc)

TODAY = dt.date(2026, 8, 25)

# 索引の見出し行。スクリプト側の定義から作る——列の順や名前を変えたら
# テストも一緒に動くようにしておく。
HEADER = ",".join(oc.COLUMNS)

# 指示欄に「埋まっている」と判定される中身。
FILLED = {
    "目的": "検証のために作った。",
    "変更範囲": "docs/ 配下のみ。",
    "禁止事項": "無し",
    "完了条件": "`python3 x.py` が終了コード 0 を返す。",
    "判断してよい範囲": "ファイル名の付け方。",
    "テスト方法": "単体テストを実装が書く。",
    "参照すべき成果物": "docs/decisions/07-task-management.md",
    "証拠": "`python3 x.py` を実行し、`OK` が出た。",
}

# 雛形のまま——案内文（HTMLコメント）だけが入っていて、未記入と判定されるもの。
TEMPLATE_COMMENT = "<!-- ここに書く -->"


# --------------------------------------------------------------------------
# 台帳を組み立てる道具
# --------------------------------------------------------------------------

def make_doc(task_id="T-001", state="実装中", updated="2026-08-25",
             owner="org-implementation", rework=0, deps="無し",
             blank_sections=(), skip_metadata=False, meta_state=None,
             meta_updated=None, blockers="無し"):
    """タスク別ファイルの中身を組み立てる。

    blank_sections に節の名前を並べると、その節は雛形の案内文だけになる
    （＝未記入として検出される）。
    """
    parts = [f"# {task_id}: 検証用のタスク", ""]

    if not skip_metadata:
        parts += [
            "## メタデータ",
            "- 作成日: 2026-08-01",
            f"- 更新日: {meta_updated or updated}",
            f"- 状態: {meta_state or state}",
            "- 優先度: 中",
            f"- 担当: {owner}",
            "- 進捗: 検証用",
            f"- 依存: {deps}",
            "- TDD適用可否: 適用",
            f"- 手戻り回数: {rework}",
            "",
        ]

    for name, body in FILLED.items():
        parts += [f"## {name}",
                  TEMPLATE_COMMENT if name in blank_sections else body,
                  ""]

    parts += ["## ブロッカー", blockers, ""]
    return "\n".join(parts)


class Fixture:
    """開発対象リポジトリ1つ分の台帳を、一時ディレクトリに作る。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.root, "docs", "tasks"))
        self.rows = []

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def task(self, task_id="T-001", created="2026-08-01", updated="2026-08-25",
             name="検証用のタスク", state="実装中", priority="中",
             owner="org-implementation", deps="無し", doc=True, **doc_kw):
        """索引へ1行足し、対応する詳細ファイルを書く。

        doc=False にすると詳細ファイルを作らない（索引だけがある状態）。
        """
        doc_rel = f"docs/tasks/{task_id}.md" if doc else ""
        self.rows.append([task_id, created, updated, name, state, priority,
                          owner, deps, doc_rel])
        if doc:
            text = make_doc(task_id=task_id, state=state, updated=updated,
                            owner=owner, deps=deps, **doc_kw)
            with open(os.path.join(self.root, doc_rel), "w", encoding="utf-8") as f:
                f.write(text)
        return self

    def write(self, header=HEADER):
        path = os.path.join(self.root, "docs", "task-list-test.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(header + "\n")
            for r in self.rows:
                f.write(",".join(r) + "\n")
        return path

    def queue(self, text):
        with open(os.path.join(self.root, "docs", "po-queue.md"),
                  "w", encoding="utf-8") as f:
            f.write(text)

    # --- 検査を走らせる ---

    def build(self, today=TODAY):
        return oc.build(self.root, self.write(), today)

    def check(self, days=2, today=TODAY):
        return oc.check(self.build(today), days)

    def summary(self, days=2, today=TODAY):
        return oc.summarize(self.build(today), days,
                            oc.count_open_questions(self.root))


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)

    def assertMentions(self, lines, *fragments):
        """どれか1行に、断片がすべて含まれていること。"""
        for line in lines:
            if all(f in line for f in fragments):
                return line
        self.fail(f"{fragments} を含む行が無い。実際の出力: {lines}")

    def assertMentionsNothing(self, lines, *fragments):
        for line in lines:
            if all(f in line for f in fragments):
                self.fail(f"{fragments} を含む行が出てはいけない: {line}")


# --------------------------------------------------------------------------
# 1. 停滞の判定と、滞留日数の集計が、同じ除外を通ること
# --------------------------------------------------------------------------

class Test停滞の除外(Base):
    """**この節が、このテストを書いた最大の理由である。**

    停滞の判定（check）と滞留日数の集計（summarize）が別々に除外条件を
    持っていた時期があり、集計の側だけが除外を欠いていた。その結果、
    依存先を待っているだけの未着手タスクが「詰まっている工程」として
    報告され、指示の足りているタスクへさらに指示を足す、という誤った
    対処へ進みかけた。
    """

    def 依存待ちの台帳(self):
        # T-001 が終わるまで T-002 は動けない。T-002 は10日間そのまま。
        return (self.fx
                .task("T-001", state="実装中", updated="2026-08-22")
                .task("T-002", state="未着手", updated="2026-08-15",
                      owner="未割当", deps="T-001（ブロッカー）"))

    def test_依存先を待っている未着手は停滞に数えない(self):
        self.依存待ちの台帳()
        stale, _, _ = self.fx.check()
        self.assertMentionsNothing(stale, "T-002")

    def test_依存先を待っている未着手は滞留日数にも数えない(self):
        """停滞に数えないものを滞留日数には数える、が実際に起きた不具合。
        両方が同じ判定（stagnation_exempt）を通っていることを固定する。"""
        self.依存待ちの台帳()
        ages = self.fx.summary()["滞留日数の中央値"]
        self.assertNotIn("未着手", ages)
        self.assertIn("実装中", ages)

    def test_依存先が完了していれば停滞に数える(self):
        """除外は「依存先が終わっていない間だけ」である。終わったのに
        動いていないなら、それは組織側の停滞であって報告すべきもの。"""
        (self.fx
         .task("T-001", state="完了", updated="2026-08-20")
         .task("T-002", state="未着手", updated="2026-08-15",
               owner="未割当", deps="T-001（ブロッカー）"))
        stale, _, _ = self.fx.check()
        self.assertMentions(stale, "T-002", "実時間基準")

    def test_ブロッカーでない依存は停滞の除外にならない(self):
        """「推奨」止まりの依存は、着手を妨げない。"""
        (self.fx
         .task("T-001", state="実装中", updated="2026-08-24")
         .task("T-002", state="未着手", updated="2026-08-15",
               owner="未割当", deps="T-001（推奨: 先に見ると早い）"))
        stale, _, _ = self.fx.check()
        self.assertMentions(stale, "T-002")

    def test_保留とPO確認待ちと完了は滞留日数に数えない(self):
        """意図して止めているものを工程の詰まりと呼ぶと、判断を誤らせる。"""
        (self.fx
         .task("T-001", state="保留", updated="2026-08-01", owner="未割当")
         .task("T-002", state="PO確認待ち", updated="2026-08-01")
         .task("T-003", state="完了", updated="2026-08-01")
         .task("T-004", state="実装中", updated="2026-08-23"))
        ages = self.fx.summary()["滞留日数の中央値"]
        self.assertEqual(list(ages), ["実装中"])


class Test依存の注記の読み取り(Base):
    """依存が「ブロッカー」なのか「推奨」なのかの読み取り。

    読み違えると、**着手できないはずのタスクが着手可能に見える。**
    しかも黙って誤読するため、誰も気づけない。実際に、別方式の台帳
    （task-cycle）から移ってきた `T-001 (blocker)` が推奨として
    読まれていた——判定が日本語の「ブロッカー」を含むかだけを
    見ていたため。

    判定不能なときにどちらへ倒すかは、損の大きさで決めてある。
    推奨と誤読して着手させるより、ブロッカーと誤読して止める方が安い。
    """

    def test_英語表記のブロッカーも依存として読む(self):
        """移行してきた台帳がこの表記で入ってくる。"""
        (self.fx
         .task("T-001", state="実装中", updated="2026-08-22")
         .task("T-002", state="未着手", updated="2026-08-15",
               owner="未割当", deps="T-001 (blocker)"))
        stale, _, warn = self.fx.check()
        self.assertMentionsNothing(stale, "T-002")
        self.assertMentionsNothing(warn, "T-002", "読めない")

    def test_英語表記の推奨は依存として読まない(self):
        """`recommended` を読み落として除外すると、逆に停滞が隠れる。"""
        (self.fx
         .task("T-001", state="実装中", updated="2026-08-24")
         .task("T-002", state="未着手", updated="2026-08-15",
               owner="未割当", deps="T-001 (recommended: faster this way)"))
        stale, _, warn = self.fx.check()
        self.assertMentions(stale, "T-002")
        self.assertMentionsNothing(warn, "T-002", "読めない")

    def test_どちらとも読めない注記は警告して安全側へ倒す(self):
        """止めるのは安全側だが、黙って止めると理由が分からない。
        除外すると同時に、書き直させる警告を必ず出す。"""
        (self.fx
         .task("T-001", state="実装中", updated="2026-08-22")
         .task("T-002", state="未着手", updated="2026-08-15",
               owner="未割当", deps="T-001（先に見ておくこと）"))
        stale, _, warn = self.fx.check()
        self.assertMentionsNothing(stale, "T-002")
        self.assertMentions(warn, "T-002", "読めない")

    def test_どちらとも読めない注記は滞留日数にも数えない(self):
        """停滞と集計が同じ判定（stagnation_exempt）を通ることの、
        判定不能の場合での固定。片方だけが安全側へ倒れると、
        「停滞していないのに詰まっている工程」として現れる。"""
        (self.fx
         .task("T-001", state="実装中", updated="2026-08-22")
         .task("T-002", state="未着手", updated="2026-08-15",
               owner="未割当", deps="T-001（先に見ておくこと）"))
        ages = self.fx.summary()["滞留日数の中央値"]
        self.assertNotIn("未着手", ages)
        self.assertIn("実装中", ages)


# --------------------------------------------------------------------------
# 2. 停滞とリマインドの切り分け
# --------------------------------------------------------------------------

class Test停滞とリマインド(Base):
    def test_更新が止まっていれば実時間基準の停滞になる(self):
        self.fx.task("T-001", state="実装中", updated="2026-08-20")
        stale, _, _ = self.fx.check(days=2)
        self.assertMentions(stale, "T-001", "5日", "実時間基準")

    def test_基準日数を変えると判定も変わる(self):
        self.fx.task("T-001", state="実装中", updated="2026-08-20")
        stale, _, _ = self.fx.check(days=10)
        self.assertEqual(stale, [])

    def test_手戻りが3回続けばサイクル基準の停滞になる(self):
        """更新が今日でも、往復が収束していなければ停滞である。"""
        self.fx.task("T-001", state="レビュー中", updated="2026-08-25", rework=3)
        stale, _, _ = self.fx.check()
        self.assertMentions(stale, "T-001", "手戻り 3回", "サイクル基準")

    def test_PO確認待ちは停滞ではなくリマインドへ回す(self):
        """組織側が止まっているのではない。混ぜると、組織の停滞件数が
        PO の未回答で水増しされる。"""
        self.fx.task("T-001", state="PO確認待ち", updated="2026-08-20",
                     blockers="Q-003 の回答待ち")
        stale, remind, _ = self.fx.check()
        self.assertEqual(stale, [])
        self.assertMentions(remind, "T-001", "5日 未回答", "Q-003")


# --------------------------------------------------------------------------
# 3. 指示欄の未記入と、完了なのに証拠が無いこと
# --------------------------------------------------------------------------

class Test指示の未記入(Base):
    def test_雛形のまま担当を付けたら4つの指示欄を検出する(self):
        """空欄が「指示の抜け」を可視化する仕組みは、空欄を機械が
        見つけられて初めて成立する。"""
        self.fx.task("T-001", state="実装中",
                     blank_sections=oc.REQUIRED_SECTIONS)
        _, _, warn = self.fx.check()
        line = self.assertMentions(warn, "T-001", "指示が未記入")
        for name in oc.REQUIRED_SECTIONS:
            self.assertIn(name, line)

    def test_案内文を消しただけの空欄も未記入とみなす(self):
        """HTMLコメントを消して何も書かない、が最も起きやすい。"""
        text = make_doc(blank_sections=["完了条件"]).replace(TEMPLATE_COMMENT, "")
        with open(os.path.join(self.fx.root, "docs", "tasks", "T-001.md"),
                  "w", encoding="utf-8") as f:
            f.write(text)
        self.fx.rows.append(["T-001", "2026-08-01", "2026-08-25", "検証用のタスク",
                             "実装中", "中", "org-implementation", "無し",
                             "docs/tasks/T-001.md"])
        _, _, warn = self.fx.check()
        self.assertMentions(warn, "T-001", "指示が未記入", "完了条件")

    def test_担当が未割当の未着手は指示欄を見ない(self):
        """まだ誰にも渡していないタスクへ指示の不足を警告すると、
        登録した瞬間から警告が出続け、警告そのものが読まれなくなる。"""
        self.fx.task("T-001", state="未着手", owner="未割当",
                     blank_sections=oc.REQUIRED_SECTIONS)
        _, _, warn = self.fx.check()
        self.assertMentionsNothing(warn, "指示が未記入")

    def test_完了なのに証拠が空なら警告する(self):
        self.fx.task("T-001", state="完了", blank_sections=["証拠"])
        _, _, warn = self.fx.check()
        self.assertMentions(warn, "T-001", "`## 証拠` が空")

    def test_メタデータ節が無ければ警告する(self):
        """この検査は、スクリプト自身が内部で足すキーによって判定が常に
        偽になり、一度も発火しない死んだコードだった時期がある。"""
        self.fx.task("T-001", skip_metadata=True)
        _, _, warn = self.fx.check()
        self.assertMentions(warn, "T-001", "「## メタデータ」節が無い")


# --------------------------------------------------------------------------
# 4. 索引と詳細ファイルの食い違い、書式、担当
# --------------------------------------------------------------------------

class Test整合性(Base):
    def test_状態が索引と詳細で食い違えば警告する(self):
        self.fx.task("T-001", state="実装中", meta_state="レビュー中")
        _, _, warn = self.fx.check()
        self.assertMentions(warn, "T-001", "状態が食い違う")

    def test_更新日が索引と詳細で食い違えば警告する(self):
        """索引が古いと停滞を誤検知する。"""
        self.fx.task("T-001", updated="2026-08-25", meta_updated="2026-08-20")
        _, _, warn = self.fx.check()
        self.assertMentions(warn, "T-001", "更新日が食い違う")

    def test_詳細ファイルが無ければ警告する(self):
        self.fx.rows.append(["T-001", "2026-08-01", "2026-08-25", "名前",
                             "実装中", "中", "org-implementation", "無し",
                             "docs/tasks/T-999.md"])
        _, _, warn = self.fx.check()
        self.assertMentions(warn, "T-001", "詳細ファイルが無い")

    def test_依存先が索引に無ければ警告する(self):
        self.fx.task("T-001", deps="T-404（ブロッカー）")
        _, _, warn = self.fx.check()
        self.assertMentions(warn, "T-001", "依存先 T-404 が索引に無い")

    def test_定義外の状態を警告する(self):
        self.fx.task("T-001", state="作業中")
        _, _, warn = self.fx.check()
        self.assertMentions(warn, "T-001", "定義された10種のどれでもない")

    def test_定義外の状態でもタスクを見失わない(self):
        """黙って捨てると、工程別の合計が未完了数と合わなくなり、
        タスクが1件行方不明になる。"""
        self.fx.task("T-001", state="作業中")
        s = self.fx.summary()
        self.assertEqual(s["工程別"].get("作業中"), 1)
        self.assertEqual(sum(s["工程別"].values()), s["未完了"])

    def test_進行中なのに担当が未割当なら警告する(self):
        self.fx.task("T-001", state="実装中", owner="未割当")
        _, _, warn = self.fx.check()
        self.assertMentions(warn, "T-001", "担当が未割当")

    def test_進行中の担当がオーケストレーターなら委譲漏れとして警告する(self):
        self.fx.task("T-001", state="実装中", owner="オーケストレーター")
        _, _, warn = self.fx.check()
        self.assertMentions(warn, "T-001", "委譲を怠っている")

    def test_日付の欄が空なら警告する(self):
        """「無し」と「不明」と「書き忘れ」を区別できないと、停滞判定が
        静かに効かなくなる。"""
        self.fx.rows.append(["T-001", "", "2026-08-25", "名前", "実装中", "中",
                             "org-implementation", "無し", ""])
        _, _, warn = self.fx.check()
        self.assertMentions(warn, "T-001", "作成日 が空欄")


# --------------------------------------------------------------------------
# 5. PO確認待ちキューの書式
# --------------------------------------------------------------------------

class TestPO確認待ちキュー(Base):
    def test_識別子と状態が同じ行に無ければ警告する(self):
        """状態を別の行に書くと、未回答が常に0件と数えられ、
        ゴール健全性の指標が静かに機能しなくなる。"""
        self.fx.queue("### Q-001 配布方式をどうするか\n- 状態: 未回答\n")
        self.assertMentions(oc.check_queue(self.fx.root),
                            "Q-001", "状態が識別子と同じ行に無い")

    def test_同じ行に状態があれば警告しない(self):
        self.fx.queue("### Q-001 [未回答] 配布方式をどうするか\n")
        self.assertEqual(oc.check_queue(self.fx.root), [])

    def test_未回答の件数を数える(self):
        self.fx.queue("### Q-001 [未回答] あれ\n"
                      "### Q-002 [回答済み] これ\n"
                      "### Q-003 [未回答] それ\n")
        self.assertEqual(oc.count_open_questions(self.fx.root), 2)

    def test_キューが無ければ件数は不明として扱う(self):
        """0件（全部答えた）と、キューがまだ無いことは別である。"""
        self.assertIsNone(oc.count_open_questions(self.fx.root))
        self.assertEqual(oc.check_queue(self.fx.root), [])


# --------------------------------------------------------------------------
# 6. 誤検出が無いこと
# --------------------------------------------------------------------------

class Test誤検出(Base):
    def test_全部埋まっている台帳では何も出ない(self):
        """検出する側のテストだけを増やすと、何にでも警告を出す
        スクリプトが「よく検出する」と評価されてしまう。"""
        (self.fx
         .task("T-001", state="完了", updated="2026-08-25")
         .task("T-002", state="実装中", updated="2026-08-25",
               deps="T-001（ブロッカー）"))
        stale, remind, warn = self.fx.check()
        self.assertEqual((stale, remind, warn), ([], [], []))


# --------------------------------------------------------------------------
# 7. コマンドとして動かしたときの終了コードと出力
# --------------------------------------------------------------------------

class TestCli(Base):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        saved_out, saved_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = oc.main(["--root", self.fx.root, "--today", TODAY.isoformat()]
                           + list(args))
        finally:
            sys.stdout, sys.stderr = saved_out, saved_err
        return code, out.getvalue(), err.getvalue()

    def test_検出が無ければ0を返す(self):
        self.fx.task("T-001", state="実装中", updated="2026-08-25").write()
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("停滞なし・警告なし", out)

    def test_検出があれば1を返す(self):
        self.fx.task("T-001", state="実装中", updated="2026-08-10").write()
        code, out, _ = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("停滞候補", out)

    def test_フックから呼ばれたら検出があっても0を返す(self):
        """Claude Code は終了コードが 0 のときだけ標準出力をセッションの
        文脈へ入れる。1 を返すと、検出があるときほど結果が届かなくなる。"""
        self.fx.task("T-001", state="実装中", updated="2026-08-10").write()
        code, out, _ = self.run_cli("--hook")
        self.assertEqual(code, 0)
        self.assertIn("停滞候補", out)

    def test_台帳の列が足りなければ2を返して標準エラーへ出す(self):
        self.fx.task("T-001").write(header="ID,状態")
        code, out, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("必要な列が無い", err)
        self.assertEqual(out, "")

    def test_台帳が壊れていてもフックからは0を返して標準出力へ出す(self):
        """台帳が壊れていることこそオーケストレーターへ届けたい情報である。
        標準エラーへ出して 2 で終わると、その情報だけが届かない。"""
        self.fx.task("T-001").write(header="ID,状態")
        code, out, err = self.run_cli("--hook")
        self.assertEqual(code, 0)
        self.assertIn("必要な列が無い", out)
        self.assertEqual(err, "")

    def test_索引が2つあれば止める(self):
        self.fx.task("T-001").write()
        with open(os.path.join(self.fx.root, "docs", "task-list-other.csv"),
                  "w", encoding="utf-8") as f:
            f.write(HEADER + "\n")
        code, _, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("索引の CSV が複数ある", err)

    def test_台帳がまだ無ければ0で静かに終わる(self):
        """組織が動き出す前にセッション開始を妨げない。"""
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("タスク台帳がまだ無い", out)

    def test_台帳が空でも0で終わる(self):
        self.fx.write()
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("タスク台帳は空", out)

    def test_基準日数が0以下なら2を返す(self):
        self.fx.task("T-001").write()
        code, _, err = self.run_cli("--days", "0")
        self.assertEqual(code, 2)
        self.assertIn("--days は1以上", err)

    def test_基準日の書式が違えば2を返す(self):
        self.fx.task("T-001").write()
        out, err = io.StringIO(), io.StringIO()
        saved_out, saved_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = oc.main(["--root", self.fx.root, "--today", "2026/08/25"])
        finally:
            sys.stdout, sys.stderr = saved_out, saved_err
        self.assertEqual(code, 2)
        self.assertIn("YYYY-MM-DD", err.getvalue())

    def test_詰まっている工程には意図して止めているものを混ぜない(self):
        """依存先を待っているだけの未着手が「詰まっている工程の候補」として
        報告されたのが、この検査を書くきっかけになった不具合である。"""
        (self.fx
         .task("T-001", state="実装中", updated="2026-08-22")
         .task("T-002", state="未着手", updated="2026-08-15",
               owner="未割当", deps="T-001（ブロッカー）")
         .write())
        code, out, _ = self.run_cli("--summary")
        self.assertEqual(code, 0)
        self.assertIn("詰まっている工程の候補: 実装中", out)
        self.assertNotIn("詰まっている工程の候補: 未着手", out)

    def test_ステータス行は1行(self):
        self.fx.task("T-001", state="実装中", updated="2026-08-25").write()
        code, out, _ = self.run_cli("--statusline")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertTrue(out.startswith("[org]"))

    def test_ステータス行は台帳が無くても何も出さない(self):
        """ステータス行は常時表示される。組織が動く前から文字を出すと
        邪魔になる。"""
        code, out, _ = self.run_cli("--statusline")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
