# 初回 DMG と公開後確認の実装計画

設計: [初回 DMG と公開後の実環境確認](../specs/2026-08-28-dmg-installer-release-design.md)

## PR と所有範囲

1 PR、版 0.1.4。契約は 1.1.0 のままで、MCP ツール変更はない。
初回インストーラーだけを追加し、権限不整合の調査とは独立に出荷する。

- 配布担当: release shell、artifact / remote verifier、新規 DMG helper、対応する release tests / fixture。
- 統合担当: VERSION、pyprojects / lock、CHANGELOG、README、配布設計と運用記録。
- 既存の native / server / app の機能コードは今回変更しない。

## 実装手順

1. 版と schema の対応、全 release assets と feed assets の区別を追加する。
   旧 schema 1 の 2 件検証を保ち、0.1.4 以降の 3 件検証・降格拒否を先にテストする。
2. DMG helper を小さく分離し、作成・署名・公証後の read-only mount 検証を実装する。
   既存 inventory を再利用し、app 内容と app ルートを含む permission mode の違いを許可しない。
   mountpoint は安全に一意作成し、所有する mount/device だけを終了・後片付けする。
3. release shell へ DMG の署名・公証・staple・封印・添付を接続する。
   公証の資格情報やエラー内容を通常ログに露出させない。
4. draft / published の全 assets 照合と匿名 DMG 取得を追加する。
   DMG を feed の 2 件検査へ混ぜず、封印した期待値を満たす前にマウントしない。
5. 版を 0.1.4 に統一し、初回 DMG / 以後 Sparkle の利用手順へ更新する。
   公開前の手動アプリ置換・実機確認を要求する旧手順は変更する。

## 検証

- Python package build、版整合、Ruff check / format、release tests、Python 全回帰。
- 配布の実物について、Swift release build、bundle inventory、Developer ID、公証、staple、Gatekeeper。
- fake tool を使う CI で、公証失敗、中身相違、外側の余分な項目、誤った symlink、attach 失敗を検証。
- 検証失敗時も所有 mount を detach すること、detach 失敗時に mountpoint を削除しないことを検証。
- app ルート・ファイル・ディレクトリの mode 不一致も拒否する。
- schema 降格、DMG 欠落／重複／改変、誤版／誤公開鍵／別 app、匿名 404、別 URL を拒否する。
- 正確性・安全性・操作運用性の内部 3 観点と同数の独立レビューを行う。
- PR 後の修正箇所再レビュー、Bot 指摘、CI を確認して squash merge する。

## 出荷と公開後の確認

1. clean な同期済み main から 0.1.4 を作成し、全署名・公証・draft assets を照合する。
2. 実機 UI 確認のためにここで保留せず、同じ release ID / tag / commit のまま公開する。
3. 匿名 feed / ZIP / DMG を検証する。
4. 公開 DMG を初回導入経路として使い、既存 app は退避してから正式にインストールする。
5. アプリ／runtime／server の版、bundled 経路、会議データ維持、起動・診断を確認する。
6. 以後の新版は Sparkle 経由で適用し、実更新と単なる同版チェックを分けて記録する。
   更新前後の app の版・build、server の版、起動結果を記録する。

退避先・旧 app の版・build・同梱 server の版・署名を記録し、初回起動失敗／更新不能時は利用者承認後に検証済み旧版へ復元する。
対象プロセス・port の終了確認と会議データ維持を復旧の前提とし、手動復元を更新成功とは扱わない。
旧版が稼働できる更新失敗は稼働版と診断ログを確認して再試行し、安全な復元元がなければ停止・報告する。

録画・OS 許可の確定は利用者の明示操作とし、許可の確認待ちで公開を停止しない。
署名や配布物の不一致は別問題として公開を停止し、公開済み asset は上書きしない。
