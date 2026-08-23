# org/ — AI開発組織の配布物

このディレクトリは、**開発対象リポジトリへ配置する組織の実行時ファイル**である。

本リポジトリ（`agents-org`）は組織の**定義**を持つ場所であり、組織が実際に動くのは開発対象リポジトリの側である。したがってここのファイルは、本リポジトリのルート `.claude/` へは置かない。理由は `docs/decisions/13-execution-form.md` の B を参照。

## 中身

| ファイル | 配置先（開発対象リポジトリ） | 内容 |
| --- | --- | --- |
| `CLAUDE.md` | リポジトリのルート | 層2の運営規約。組織の構成、標準フロー、共通規約、Git、直接通信 |
| `glossary.md` | `docs/` | **用語集。** 一般的な意味と違う語、範囲が曖昧になる語、このプロジェクトの語。最後の節はプロジェクト開始後にオーケストレーターが育てる |
| `rules/org-task-ledger.md` | `.claude/rules/` | タスク台帳の書き方（CSV索引 + タスク別Markdown） |
| `rules/org-escalation.md` | `.claude/rules/` | エスカレーションの4段階、共通停止条件、PO確認待ちキュー |
| `agents/org-design.md` | `.claude/agents/` | 設計エージェント |
| `agents/org-implementation.md` | `.claude/agents/` | 実装エージェント |
| `agents/org-review.md` | `.claude/agents/` | レビューエージェント（**編集系ツールを持たない**） |
| `agents/org-test.md` | `.claude/agents/` | テストエージェント |
| `agents/org-documentation.md` | `.claude/agents/` | ドキュメントエージェント |
| `skills/org-orchestrate/` | `.claude/skills/` | オーケストレーターの手順 |
| `skills/org-session-resume/` | `.claude/skills/` | セッション再開の手順 |
| `skills/org-first-run-check/` | `.claude/skills/` | **初回運用の点検手順。** 最初のタスクが1件完了したら使う |
| `scripts/org-check.py` | `.claude/scripts/` | **停滞検知と整合性検査。** Python 3.8 以降、標準ライブラリのみ |
| `settings.snippet.json` | — | セッション開始時に上のスクリプトを走らせる設定。**既存の設定へ追記する**（後述） |

**オーケストレーターのエージェント定義ファイルは無い。** オーケストレーターは**メインセッション自身**である。サブエージェントは `AskUserQuestion` を使えず、PO へ問い合わせできないため、サブエージェントであり得ない。

## 導入

```sh
# 開発対象リポジトリのルートで
mkdir -p .claude/agents .claude/rules .claude/skills
mkdir -p docs
cp    /path/to/agents-org/org/glossary.md        docs/
cp -r /path/to/agents-org/org/agents/*        .claude/agents/
cp -r /path/to/agents-org/org/rules/*         .claude/rules/
cp -r /path/to/agents-org/org/skills/*        .claude/skills/
mkdir -p .claude/scripts
cp    /path/to/agents-org/org/scripts/org-check.py  .claude/scripts/
```

`org/CLAUDE.md` は、開発対象リポジトリのルート `CLAUDE.md` へ**追記**する。既に `CLAUDE.md` があれば置き換えず、組織の節として足す。開発対象そのものの規約（技術スタック、ビルド手順、コーディング規約）は、その下へ書く。

### 既存拡張との共存

Everything Claude Code / Superpowers 等が導入済みでも共存する前提である。

- すべて `org-` プレフィックスを持つ。**既存の commands / skills / hooks を上書き・削除しない**
- hook を足す場合は、既存エントリを**置換せず追記**する
- 名前が衝突したら**組織側が譲る**（`org-` の後ろを変える）

### 停滞検知を自動で走らせる

`settings.snippet.json` の中身を、開発対象リポジトリの `.claude/settings.json` へ**追記**する。

**ファイルごと上書きしない。** 既存の設定（他の拡張が入れた hook を含む）を消してしまう。`hooks` の項目が既にあるなら、`SessionStart` の配列へ1件足す形にする。

これで、セッションが始まるたびに台帳が検査され、結果が Claude の文脈へ入る。

**`CLAUDE.md` へ書くだけでは駄目な理由**: `CLAUDE.md` は読まれる文脈であって、実行される設定ではない。「セッション開始時に停滞を確認せよ」と書いても、実行されない回が出る。必ず走らせたいものは hook にする。

### スクリプトの使い方

```sh
python3 .claude/scripts/org-check.py              # 停滞検知と整合性検査
python3 .claude/scripts/org-check.py --summary    # ゴール健全性の指標を集計
python3 .claude/scripts/org-check.py --statusline # 1行にまとめる（ステータス行向け）
python3 .claude/scripts/org-check.py --days 3     # 停滞と判定する日数を変える（既定 2）
```

終了コードは、検出なしで `0`、停滞や警告ありで `1`、台帳が読めないときに `2`。

**このスクリプトは判定するだけで、対処はしない。** 何をするかはオーケストレーターが決める。スクリプトが勝手に担当を変えると、なぜそうなったかが記録に残らないため。

## まだ入っていないもの

- 人間用ビュー（README / handbook）の生成スクリプト。`docs/decisions/06-documentation-agent.md` の確定事項 A

## 配布方式について

現状は手動コピーである。Claude Code のプラグイン機構でまとめれば「ファイル追加だけで導入できる」と「既存拡張と共存する」の両方をより強く満たせる可能性があるが、MVP では手動コピーで足りると判断した。**PO の判断を仰ぐべき論点として残してある**（`docs/decisions/13-execution-form.md` の未確定事項）。
