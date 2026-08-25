#!/usr/bin/env python3
"""タスク別・担当エージェント別・モデル別のトークン消費量を集計する。

何のためか
----------
Claude Code の定額プランには、1日・1週間で使える量の上限がある。その枠内で
こなせるタスクを増やすには、まず「どのタスクの、どの担当が、どのモデルで、
どれだけ食っているか」が見えていなければならない。このスクリプトはその
可視化だけを行う。削減策は数字を見てから決める。

どこからデータを取るか
----------------------
新しく記録を取る仕組みは作らない。Claude Code が既に書き出している会話記録
（1行1イベントの JSONL 形式のファイル）を読む。置き場は次のとおり。

    ~/.claude/projects/<作業ディレクトリを符号化した名前>/
        <セッションID>.jsonl                 メインセッション（オーケストレーター）
        <セッションID>/subagents/
            agent-XXXX.jsonl                 担当エージェント1体分の会話
            agent-XXXX.meta.json             その担当エージェントの種別（agentType）

モデルの返答1回ごとに、使ったトークン数が4種類（新規入力・出力・キャッシュ
書込・キャッシュ読出）に分かれて記録されている。担当エージェント別の集計は、
こちらで何も仕込まなくてもこの構造だけで取れる。

<作業ディレクトリを符号化した名前> の規則は推測しない。同じパスでも大文字
小文字が違うだけで別のディレクトリになっている実例があるため、規則から
組み立てると取りこぼす。代わりに、記録の中に入っている `cwd`（そのセッション
の作業ディレクトリ）を読んで突き合わせる。

タスクへの紐付け
----------------
「このトークンは T-007 のもの」という情報は記録に無いので、指示文から拾う。

  担当エージェント: 記録の先頭にある指示文から `T-007` の形を探す。担当
      エージェントへの指示は自己完結でなければならない（会話履歴を引き継げ
      ないため）ので、指示文にはタスクIDがほぼ必ず書かれている。**正確。**

  メインセッション: 時系列に読み進め、タスクIDが現れたら、それ以降の返答を
      そのタスクのものとみなす。次のIDが現れたら切り替える。**近似である。**
      最初のIDが現れる前の分は `タスク外` に入る。オーケストレーターがタスク
      に紐付かない運営作業へどれだけ使っているかも、これで見える。

Python 3.8 以降。標準ライブラリのみ。追加インストールは要らない。

使い方:
    python3 org-tokens.py --update        会話記録を読んで台帳を更新する
    python3 org-tokens.py                 タスク別に集計して表示する（既定）
    python3 org-tokens.py --by agent      担当エージェント別
    python3 org-tokens.py --by model      モデル別
    python3 org-tokens.py --task T-007    1タスクの内訳（担当別・モデル別）
    python3 org-tokens.py --statusline    1行にまとめる（ステータス行向け）
    python3 org-tokens.py --root path/to/repo
    python3 org-tokens.py --update --hook フックから呼ぶとき

`python3` という名前のコマンドが無い環境（Windows の標準的な導入ではこれが
普通）では `python` に読み替える。

終了コード:
    0  正常
    1  台帳がまだ空、または集計対象が無い
    2  台帳が読めない、書けない

--hook を付けると、何が起きても 0 で終わり、出力を数行に抑える。Claude Code
のフックは、終了コードが 0 のときだけ標準出力をセッションの文脈へ入れる仕様
のため——そのままだと、伝えたいことがあるときほど届かなくなる。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import json
import os
import re
import sys
import unicodedata

# --- 台帳の書式（README と .claude/rules/org-task-ledger.md に一致させること） ---

COLUMNS = [
    "タスクID", "担当", "モデル",
    "入力", "出力", "キャッシュ書込", "キャッシュ読出", "合計",
    "返答回数", "セッションID", "記録元", "最終更新",
]

# トークンの費目。会話記録の usage 欄のキーと、台帳の列名の対応。
# キャッシュ読出が支配的になりやすい——会話が伸びるほど、毎回の返答で
# それまでのやりとり全体をもう一度モデルに読ませることになるため。
KINDS = ["入力", "出力", "キャッシュ書込", "キャッシュ読出"]

# 数値の列。読み込み時に整数へ直す。
NUMERIC = KINDS + ["合計", "返答回数"]

ORCHESTRATOR = "オーケストレーター"   # メインセッションの担当名
MAIN_SOURCE = "main"                  # メインセッションの記録元名
OUTSIDE_TASK = "タスク外"             # どのタスクにも紐付かない分
UNKNOWN_TASK = "タスク不明"           # 担当エージェントの指示文にIDが無かった

TASK_ID = re.compile(r"T-\d+")

# 会話記録の先頭を何行まで見て作業ディレクトリを探すか。
# 全部読むと、対象外のセッションにも数メガバイトの読み込みが発生する。
CWD_PROBE_LINES = 40


class LedgerError(Exception):
    """台帳が読めない・書けない。終了コード 2 になる。"""


# --------------------------------------------------------------------------
# 会話記録の在り処を突き止める
# --------------------------------------------------------------------------

def config_dir() -> str:
    """Claude Code の設定ディレクトリ。環境変数で移せるので、それを優先する。"""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )


def same_path(a: str, b: str) -> bool:
    """2つのパスが同じ場所を指すか。Windows は大文字小文字を区別しない。"""
    def norm(p):
        return os.path.normcase(os.path.normpath(os.path.abspath(p)))
    return norm(a) == norm(b)


def transcript_cwd(path: str) -> str | None:
    """会話記録が、どの作業ディレクトリのものかを読む。先頭の数行だけ見る。"""
    try:
        with open(path, encoding="utf-8") as f:
            for _ in range(CWD_PROBE_LINES):
                line = f.readline()
                if not line:
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("cwd"):
                    return entry["cwd"]
    except OSError:
        return None
    return None


def find_sessions(root: str, transcripts: str | None = None) -> list[dict]:
    """このリポジトリで動いたセッションを全部見つける。

    返すのは {セッションID, メイン記録のパス, 担当エージェント記録の置き場} の一覧。
    """
    base = transcripts or os.path.join(config_dir(), "projects")
    if not os.path.isdir(base):
        return []

    found = []
    for path in sorted(glob.glob(os.path.join(base, "*", "*.jsonl"))):
        cwd = transcript_cwd(path)
        if not cwd or not same_path(cwd, root):
            continue
        session_id = os.path.basename(path)[: -len(".jsonl")]
        found.append({
            "id": session_id,
            "main": path,
            "subagents": os.path.join(os.path.dirname(path), session_id, "subagents"),
        })
    return found


# --------------------------------------------------------------------------
# 会話記録を読む
# --------------------------------------------------------------------------

def read_entries(path: str):
    """会話記録を1行ずつ読む。壊れた行は黙って飛ばす。

    記録は書き込みの途中で読まれることがある（セッションが動いている最中に
    フックから呼ばれる）。最後の1行が途中までしか書かれていない状態は正常な
    出来事なので、そこで失敗しない。
    """
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    yield entry
    except OSError:
        return


def message_text(entry: dict) -> str:
    """タスクIDを探すための文字列。中身の構造は問わず、まとめて文字列にする。

    モデルが書いた文、考えた内容、道具を呼んだときの引数（編集するファイルの
    パスなど）が全部入る。`docs/tasks/T-007.md` を編集した、という事実も
    タスクIDの手がかりになるので、引数まで含めるのは意図的である。
    """
    message = entry.get("message") or {}
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def is_human_turn(entry: dict) -> bool:
    """人間（またはオーケストレーター）が書いた入力か。

    種別が user のものには、道具の実行結果が返ってきただけの行も含まれる。
    そこにはファイルの中身がそのまま入っていて、無関係なタスクIDを大量に
    含みうる——タスクの取り違えの原因になるので、除く。
    実行結果は content が配列になり、人間の入力は文字列になる。
    """
    return entry.get("type") == "user" and isinstance(
        (entry.get("message") or {}).get("content"), str
    )


def usage_of(entry: dict) -> tuple | None:
    """モデルの返答1回分から (モデル名, 費目別トークン数) を取り出す。"""
    if entry.get("type") != "assistant":
        return None
    message = entry.get("message") or {}
    usage = message.get("usage")
    model = message.get("model")
    if not usage or not model:
        return None

    # キャッシュ書込は保持時間（5分 / 1時間）で内訳が出ている。今は量だけ見る
    # ので合算する。金額へ直す段になったら価格が違うので、そこでは分けること。
    creation = usage.get("cache_creation") or {}
    written = (creation.get("ephemeral_5m_input_tokens", 0) or 0) \
        + (creation.get("ephemeral_1h_input_tokens", 0) or 0)
    if not written:
        written = usage.get("cache_creation_input_tokens", 0) or 0

    return model, {
        "入力": usage.get("input_tokens", 0) or 0,
        "出力": usage.get("output_tokens", 0) or 0,
        "キャッシュ書込": written,
        "キャッシュ読出": usage.get("cache_read_input_tokens", 0) or 0,
    }


def blank_row(task: str, owner: str, model: str, session: str, source: str) -> dict:
    row = {"タスクID": task, "担当": owner, "モデル": model,
           "セッションID": session, "記録元": source, "最終更新": ""}
    for key in NUMERIC:
        row[key] = 0
    return row


def add(rows: dict, key: tuple, task, owner, model, session, source, counts) -> None:
    row = rows.setdefault(key, blank_row(task, owner, model, session, source))
    for kind in KINDS:
        row[kind] += counts[kind]
    row["合計"] += sum(counts.values())
    row["返答回数"] += 1


def scan_main(session: dict) -> dict:
    """メインセッション（オーケストレーター）の消費を、タスク別・モデル別に分ける。

    タスクIDが現れたら、それ以降の返答をそのタスクのものとみなす。近似である。
    正確な紐付けは記録に無い以上ここが限界だが、「どのタスクが重かったか」の
    比較には十分効く。
    """
    rows: dict = {}
    current = OUTSIDE_TASK
    for entry in read_entries(session["main"]):
        if entry.get("type") == "assistant" or is_human_turn(entry):
            ids = TASK_ID.findall(message_text(entry))
            if ids:
                current = ids[-1]
        got = usage_of(entry)
        if got:
            model, counts = got
            add(rows, (current, model), current, ORCHESTRATOR, model,
                session["id"], MAIN_SOURCE, counts)
    return rows


def agent_task(path: str) -> str:
    """担当エージェントの記録から、担当したタスクIDを取り出す。

    先頭の指示文だけを見る。指示は自己完結で書かれるという規約があるので、
    タスクIDはそこに書かれている。以降の会話（読んだファイルの中身など）まで
    見に行くと、無関係なIDを拾って取り違える。
    """
    for entry in read_entries(path):
        if entry.get("type") == "user":
            ids = TASK_ID.findall(message_text(entry))
            return ids[0] if ids else UNKNOWN_TASK
    return UNKNOWN_TASK


def agent_sources(session: dict) -> list[tuple]:
    """担当エージェントの記録を (記録元の名前, 記録のパス, 担当名) で列挙する。"""
    out = []
    for meta_path in sorted(glob.glob(os.path.join(session["subagents"], "*.meta.json"))):
        log_path = meta_path[: -len(".meta.json")] + ".jsonl"
        if not os.path.exists(log_path):
            continue
        try:
            with open(meta_path, encoding="utf-8") as f:
                owner = (json.load(f) or {}).get("agentType") or "不明"
        except (OSError, ValueError):
            owner = "不明"
        out.append((os.path.basename(log_path)[: -len(".jsonl")], log_path, owner))
    return out


def scan_agents(session: dict) -> dict:
    """担当エージェント1体ごとの消費を、タスク別・モデル別に分ける。"""
    rows: dict = {}
    for source, log_path, owner in agent_sources(session):
        task = agent_task(log_path)
        for entry in read_entries(log_path):
            got = usage_of(entry)
            if got:
                model, counts = got
                add(rows, (source, task, model), task, owner, model,
                    session["id"], source, counts)
    return rows


# --------------------------------------------------------------------------
# 台帳の読み書き
# --------------------------------------------------------------------------

def ledger_path(root: str) -> str:
    """トークン消費の台帳の置き場。無ければ、これから作る名前を返す。

    索引の CSV（docs/task-list-{project-name}.csv）と同じ名前の付け方にそろえる。
    タスク台帳とは別ファイルにする——タスク台帳は「何をやるか」の記録で、
    書く主体はオーケストレーターだけと決まっている。こちらは「いくら食ったか」
    の記録で、スクリプトが機械的に上書きする。混ぜると、集計のたびにタスク台帳
    の差分が汚れ、誰が何を書き換えたのかが追えなくなる。
    """
    hits = sorted(glob.glob(os.path.join(root, "docs", "token-usage-*.csv")))
    if len(hits) > 1:
        raise LedgerError(
            "トークン台帳が複数ある。1つに絞ること:\n  "
            + "\n  ".join(os.path.relpath(h, root) for h in hits)
        )
    if hits:
        return hits[0]

    # まだ無い。タスク台帳があれば、そこから同じ {project-name} をもらう。
    index = sorted(glob.glob(os.path.join(root, "docs", "task-list-*.csv")))
    if index:
        name = os.path.basename(index[0])[len("task-list-"):-len(".csv")]
    else:
        name = os.path.basename(os.path.abspath(root))
    return os.path.join(root, "docs", "token-usage-%s.csv" % name)


def read_ledger(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        # utf-8-sig にしておくと、表計算ソフトが付ける先頭の目印があっても読める。
        with open(path, encoding="utf-8-sig", newline="") as f:
            raw = list(csv.DictReader(f))
    except OSError as e:
        raise LedgerError("台帳を開けない: %s" % e)
    except csv.Error as e:
        raise LedgerError("台帳の書式が壊れている: %s" % e)

    if not raw:
        return []
    missing = [c for c in COLUMNS if c not in raw[0]]
    if missing:
        raise LedgerError(
            "台帳に必要な列が無い: " + " / ".join(missing)
            + "\n見出し行はこの%d列にすること: " % len(COLUMNS) + ",".join(COLUMNS)
        )

    rows = []
    for r in raw:
        row = {k: (r.get(k) or "").strip() for k in COLUMNS}
        for key in NUMERIC:
            try:
                row[key] = int(row[key] or 0)
            except ValueError:
                raise LedgerError(
                    "台帳の %s 列に数値でない値がある: %r（タスク %s）"
                    % (key, row[key], row.get("タスクID"))
                )
        rows.append(row)
    return rows


def sort_key(row: dict) -> tuple:
    """並び順。タスクIDは T-7 と T-70 が混ざっても数の順に並べる。"""
    m = re.match(r"T-(\d+)$", row["タスクID"])
    head = (0, int(m.group(1)), "") if m else (1, 0, row["タスクID"])
    return (head, row["担当"], row["モデル"], row["セッションID"], row["記録元"])


def write_ledger(path: str, rows: list[dict]) -> None:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # newline="" は csv モジュールの作法。付けないと Windows で空行が挟まる。
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            for row in sorted(rows, key=sort_key):
                writer.writerow({k: row.get(k, "") for k in COLUMNS})
    except OSError as e:
        raise LedgerError("台帳を書けない: %s" % e)


def update(root: str, transcripts: str | None) -> tuple:
    """会話記録を読み直して台帳を更新する。何度走らせても結果は同じ。

    同じセッションの同じ記録元の行は、古いものを捨てて入れ直す。会話が伸びれば
    数字は増えるし、タスクの紐付けも後から変わりうるため、足し込みではなく
    置き換えにする。足し込みにすると、フックが走るたびに二重計上になる。

    記録がもう残っていないセッションの行は、そのまま保存する。再計算できない
    ものを黙って消すと、過去の消費が台帳から消えてしまう。
    """
    path = ledger_path(root)
    existing = read_ledger(path)

    sessions = find_sessions(root, transcripts)

    # 今回読み直した (セッション, 記録元) の組。ここに入っているものだけ入れ替える。
    rescanned = set()
    fresh: dict = {}
    for session in sessions:
        rescanned.add((session["id"], MAIN_SOURCE))
        for source, _log, _owner in agent_sources(session):
            rescanned.add((session["id"], source))
        for rows in (scan_main(session), scan_agents(session)):
            for row in rows.values():
                fresh[(row["セッションID"], row["記録元"],
                       row["タスクID"], row["モデル"])] = row

    before = {(r["セッションID"], r["記録元"], r["タスクID"], r["モデル"]) for r in existing}
    today = dt.date.today().isoformat()

    merged = [r for r in existing if (r["セッションID"], r["記録元"]) not in rescanned]
    for row in fresh.values():
        row["最終更新"] = today
        merged.append(row)

    write_ledger(path, merged)
    added = len([k for k in fresh if k not in before])
    scanned_total = sum(r["合計"] for r in fresh.values())
    return path, added, scanned_total, len(sessions)


# --------------------------------------------------------------------------
# 集計と出力
# --------------------------------------------------------------------------

def width(text) -> int:
    """端末上での表示幅。日本語は1文字で2つ分の幅を取る。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))


def pad(text, size: int) -> str:
    return str(text) + " " * max(0, size - width(text))


def group(rows: list[dict], key: str) -> list[dict]:
    """指定した列でまとめる。消費の大きい順に並べて返す。"""
    out: dict = {}
    for row in rows:
        acc = out.setdefault(row[key], {key: row[key], "担当数": set(), "モデル数": set()})
        for k in NUMERIC:
            acc[k] = acc.get(k, 0) + row[k]
        acc["担当数"].add(row["担当"])
        acc["モデル数"].add(row["モデル"])
    for acc in out.values():
        acc["担当数"] = len(acc["担当数"])
        acc["モデル数"] = len(acc["モデル数"])
    return sorted(out.values(), key=lambda a: -a["合計"])


SHOW = ["合計", "出力", "キャッシュ書込", "キャッシュ読出", "入力"]


def print_table(title: str, key: str, groups: list[dict], extra: list[str]) -> None:
    if not groups:
        print("■ %s: 該当なし" % title)
        return

    headers = [key] + SHOW + extra
    body = [[g[key]] + ["{:,}".format(g[c]) for c in SHOW] + [str(g[c]) for c in extra]
            for g in groups]
    total = ["合計"] + ["{:,}".format(sum(g[c] for g in groups)) for c in SHOW] \
        + ["" for _ in extra]

    sizes = [max(width(r[i]) for r in [headers] + body + [total])
             for i in range(len(headers))]

    def line(cells):
        return "  " + pad(cells[0], sizes[0]) + "".join(
            " " * (sizes[i] - width(cells[i]) + 2) + str(cells[i])
            for i in range(1, len(cells)))

    rule = "  " + "-" * (sum(sizes) + 2 * (len(sizes) - 1))
    print("■ " + title)
    print(line(headers))
    print(rule)
    for row in body:
        print(line(row))
    print(rule)
    print(line(total))


def print_report(rows: list[dict], by: str, task: str | None) -> int:
    if task:
        subset = [r for r in rows if r["タスクID"] == task]
        if not subset:
            print("%s の記録が台帳に無い。`--update` で会話記録を読み込むこと。" % task)
            return 1
        print_table("%s の内訳（担当別）" % task, "担当", group(subset, "担当"), ["返答回数"])
        print()
        print_table("%s の内訳（モデル別）" % task, "モデル", group(subset, "モデル"), ["返答回数"])
        return 0

    column = {"task": "タスクID", "agent": "担当", "model": "モデル"}[by]
    extra = {"task": ["担当数", "返答回数"], "agent": ["返答回数"], "model": ["返答回数"]}[by]
    print_table("トークン消費（%s別）" % column, column, group(rows, column), extra)
    print()
    print("  キャッシュ読出は「毎回の返答のたびに、それまでの会話全体をもう一度")
    print("  モデルに読ませている分」である。ここが大きいタスクは、会話が長く")
    print("  なりすぎているか、担当エージェントへ渡している指示が重い。")
    print()
    print("  合計は4費目の素の足し算である。定額プランの消費枠はモデルごとに")
    print("  重みが違うため、モデルをまたいだ合計をそのまま枠の消費量とは読めない。")
    print("  モデル別（--by model）と併せて見ること。")
    return 0


def print_statusline(rows: list[dict]) -> int:
    if not rows:
        print("[org] トークン記録なし")
        return 0
    total = sum(r["合計"] for r in rows)
    parts = ["累計 %.1fM" % (total / 1e6)]
    tasks = [g for g in group(rows, "タスクID") if g["タスクID"] != OUTSIDE_TASK]
    if tasks:
        parts.append("最大 %s %.1fM" % (tasks[0]["タスクID"], tasks[0]["合計"] / 1e6))
    if total:
        read = sum(r["キャッシュ読出"] for r in rows)
        parts.append("ｷｬｯｼｭ読出 %d%%" % round(read * 100.0 / total))
    print("[org] " + " · ".join(parts))
    return 0


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="タスク別・担当エージェント別・モデル別のトークン消費量を集計する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--root", default=".", help="リポジトリのルート（既定: カレントディレクトリ）")
    ap.add_argument("--update", action="store_true", help="会話記録を読んで台帳を更新する")
    ap.add_argument("--by", choices=["task", "agent", "model"], default="task",
                    help="集計の軸（既定: task）")
    ap.add_argument("--task", help="1タスクの内訳を出す（例: T-007）")
    ap.add_argument("--statusline", action="store_true", help="1行にまとめる")
    ap.add_argument("--transcripts", help="会話記録の置き場（既定: ~/.claude/projects）")
    ap.add_argument("--hook", action="store_true",
                    help="フックから呼ぶ。何が起きても終了コード 0 を返し、出力を数行に抑える")
    args = ap.parse_args(argv)

    try:
        if args.update:
            path, added, scanned, sessions = update(args.root, args.transcripts)
            if sessions == 0:
                if not args.hook:
                    print("このリポジトリの会話記録が見つからない。"
                          "`--transcripts` で置き場を指定できる。")
                return 0 if args.hook else 1
            print("トークン台帳を更新: %s（セッション %d件 / 計 %s トークン / 新規 %d行）"
                  % (os.path.relpath(path, args.root), sessions,
                     "{:,}".format(scanned), added))
            if args.hook:
                return 0

        rows = read_ledger(ledger_path(args.root))
    except LedgerError as e:
        message = "トークン台帳: %s" % e
        if args.hook:
            # 終了コードが 0 のときだけ標準出力がセッションの文脈へ入る。
            # 台帳が壊れていることこそ伝えたい情報なので、標準出力へ出して 0 で終わる。
            print(message)
            return 0
        print(message, file=sys.stderr)
        return 2

    if not rows:
        if args.statusline:
            return print_statusline(rows)
        if not args.hook:
            print("トークン台帳がまだ空。`--update` で会話記録を読み込むこと。")
        return 0 if args.hook else 1

    if args.statusline:
        return print_statusline(rows)
    code = print_report(rows, args.by, args.task)
    return 0 if args.hook else code


if __name__ == "__main__":
    sys.exit(main())
