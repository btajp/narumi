# 録画権限セットアップ実装計画

設計: [録画権限のセットアップ導線](../specs/2026-08-28-recording-permission-setup-design.md)

## PR と版

1 PR で診断 UI、MCP 契約、常駐 server、native helper、製品 CLI を同時に提供する。
アプリと Python パッケージの版は `0.1.3`、契約は `1.1.0`。
新たなグローバル依存や ML 依存は加えない。既存の署名・公証・更新経路を利用する。

## 実装順と所有範囲

1. 契約: `contracts/defs/common.json`、新 tool、`get_server_info`、manifest を先に更新。
   契約テストへ正常例、入力の厳格拒否、従来の空入力、処理中フラグを追加し、
   `uv run pytest pipeline/tests/contracts` を通してから下記実装へ進む。
2. native 担当: `NarumiRecorderKit` の許可モデル・操作・引数、recorder executable とそのテスト。
   固定 URL と録画なしのコマンドを実装する。OS 呼出は fake へ注入する。
3. server 担当: `RecordingController`、permission handler、server info、tool 登録、CLI、
   `server/tests` の fake と回帰。ゲートと所有子プロセスの終了処理を実装する。
   同一冪等キーの待機再送も新 permission tool に限り非ブロッキングにする。
4. app 担当: `NarumiMenuBarCore` の型・表示状態・排他、`NarumiMenuBar` の client／transport／
   host actions／診断／録画入口、および対応する Core tests。
   native と app は別ファイルを所有し、同じ Swift build directory の同時利用を避ける。
5. 統合担当: `VERSION`、各 pyproject／lock、CHANGELOG、利用手順、契約と表面の整合。
   完了した担当範囲は無断で書き換えず、必要な修正を担当者へ戻す。

## 検証ゲート

- build: Python パッケージ構築、Swift build。Swift の型検査もここで行う。
- lint: Ruff check、format check、変更したシェルがあれば構文確認。
- tests: 契約、fake native、controller／handler／CLI、UI の純粋状態、Python／Swift 全回帰。
- 新しい挙動は unit と fake 統合の両方で検証する。実 TCC や実録画には依存しない。
- サーバーの子プロセス timeout／shutdown、録画開始との双方向競合、HTTP 結果不明時の
  UI 保留、同一 ID 並送と初回失敗、設定復帰時の fresh 読取、旧応答破棄を重点確認する。
- 接続不能時にも接続の再確認を残し、所有プロセス群の終了確認後だけ明示再起動を許可する。
  外部サーバーや生存不明な子プロセスは停止・解除しない回帰を追加する。
- server 起動単位の `server_instance_id` に未解決操作を結び付け、同一 ID の fresh 応答
  だけで保留を解除する。別サーバー・ID 欠落の idle 応答では解除しない回帰を追加する。
- 旧サーバーでは空の診断入力だけを使い、新しい引数・ツールを送らず更新を案内するテストを追加する。
- 追加したログとエラー文に会議内容・秘密情報が出ないことを確認する。
- 最終 diff と秘密情報を点検し、正確性・安全性・UX 運用の独立レビューを通す。
- PR 作成後にも修正点中心の独立レビューを行い、CI／Bot 指摘を確認して squash merge する。

## 出荷と実機確認

1. clean かつ同期済みの main から `0.1.3` を Developer ID 署名、公証、staple して作る。
   Sparkle 署名と ZIP、feed、source の一致を検査し、まず GitHub draft にする。
2. 差し替え直前に稼働サーバーの録画・ジョブ状態を確認する。忙しければ保留する。
   許可操作が待機中の場合や、録画・ジョブ・許可操作の状態が確認できない場合も置換しない。
   旧アプリとその所有サーバーを正常終了させ、対象 PID と子プロセス群の終了、待受ポートの解放を
   確認する。外部所有プロセスは停止しない。停止を確認できなければ置換しない。
   その後、旧アプリを退避して `/Applications/narumi.app` を置き換え、会議データを変更しない。
3. bundled runtime、アプリ／server／契約版、署名を確認する。新しい診断画面の表示、
   設定画面を開く導線、read-only 再確認を実機で検査する。
4. 許可の付与を自動化しない。OS が再起動を要求しても無断で会議を開始しない。
   利用者がまだ許可していない場合はその点を明示し、録画が動いたとは報告しない。
5. 配布物の確認後に GitHub release を公開し、匿名の feed と ZIP を再検証する。
   旧版からの Sparkle 実適用は、同版での更新確認や feed 検証と区別して報告する。

## リスクと保留事項

- macOS の項目名と URL アンカーは版依存。アプリ内に手動の辿り方も残す。
- Python 経由 helper の TCC 帰属は署名・起動経路を維持し、実 OS の付与は利用者が判断する。
- 権限付与後も録画権限が更新されなければ再起動が必要な場合がある。
- Notion との同時録画は今回の fake テスト・設定画面確認では保証しない。
