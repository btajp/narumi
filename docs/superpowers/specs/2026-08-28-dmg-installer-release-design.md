# 初回 DMG と公開後の実環境確認（2026-08-28）

## 目的と順序

初回は GitHub Releases の DMG から正式にインストールする。
以後は「リリース公開 → インストール済みアプリの Sparkle 更新 → 実環境確認」の順にする。
開発ビルドや draft の app を毎回 Applications へ直接コピーする運用には戻さない。

署名・公証・配布内容の照合と CI は公開前に行う。
録画権限や UI の実機確認は公開後に行い、その確認待ちで配布を保留しない。
公開物の署名・ハッシュ・内容が不正な場合は公開を止める。

## 配布物

- 初回導入: `narumi-<version>.dmg`。標準のドラッグ式 DMG とし、pkg や独自インストーラーは作らない。
- 更新: 従来どおり `narumi-<version>.zip` と `appcast.xml`。Sparkle の enclosure は ZIP だけ。
- DMG は最終 ZIP から検証済みの同じ app を取り出して作る。ソースや別ビルドから作り直さない。
- DMG の内容は `narumi.app` と `Applications -> /Applications`。app 内部の symlink 制約は緩めない。
- DMG を `feed/` へ入れず、出荷専用ディレクトリ内の `installer/` に置く。appcast 生成に DMG を混入させない。

初回導入と更新で署名主体、bundle identifier、同梱 runtime、公開鍵を共通にする。
Developer ID Application と既存の Apple 公証資格情報を使い、Installer 証明書や新規 CLI は要求しない。

## 版と封印形式

0.1.3 の公開物は変更しない。0.1.4 から DMG を追加する。

- 0.1.3 以前: `schema_version=1`、release assets は ZIP と appcast の 2 件。
- 0.1.4 以降: `schema_version=2`、release assets は ZIP・appcast・版付き DMG の 3 件。
- 版に応じた schema を必須とし、欠落・余分・重複・schema の降格を拒否する。
- `feed/` の 2 ファイル厳密検査は維持する。DMG の長さ・SHA256・公証結果・内容照合結果を別途封印する。
- 出荷元 main/commit、アプリ版、単調増加 build、公開鍵、追跡ソースの検査を維持する。

## 作成と検証

1. 従来の app の Developer ID 署名・公証・staple・ZIP 再展開検査を完了する。
2. 確定した ZIP の app と Applications リンクから UDZO DMG を作り、DMG 自体を署名する。
3. DMG 自体も公証・staple し、署名・公証チケットとディスクイメージを検証する。
4. 安全に一意作成した専用の一時 mountpoint に read-only / no-browse / no-auto-open でマウントする。
5. 外側の項目、app の inventory・版・公開鍵・署名・staple を確認する。
   相対 path、種類、内容 hash、symlink target、app ルートを含む permission mode を ZIP の app と照合する。
   hdiutil が必須生成する filesystem metadata がある場合だけ、確認した正確な名前を許可する。
6. 自分の attach 結果に結び付いた mount/device だけを detach する。
   detach 失敗時は mountpoint を再帰削除せず、場所を保持して失敗として返す。
7. appcast は確定済み ZIP から作り、DMG を含む今回の全 assets を封印して draft に添付する。
8. 実 draft assets を再取得して URL・ID・長さ・hash・内容を照合した後、同 release ID を公開する。
   公開時は `tag_name` と `target_commitish` を固定値のまま明示し、placeholder tag へ変えない。
9. 匿名で latest feed、その enclosure ZIP、版付き DMG を取得して同一性を確認する。

取得した DMG は期待長・SHA256 の一致を確認してからマウントする。
公開後の assets は上書きせず、修正は次の版で行う。

## 実アプリへの反映

初回だけ、公開済み DMG をダウンロードし、旧 app の退避を残して Applications へ導入する。
退避先は利用者の Application Support 内に一意作成した `narumi/app-backups/<版>-<日時>/` とし、
旧 app の版・build・同梱 server の版・署名主体と退避先を記録する。正式署名・公証済みの旧版だけを復元候補とする。
コピー前に録画・ジョブ・許可操作の idle を確認し、旧 app と所有 server を正常終了させる。
対象 PID と所有 process group の終了、待受 port の解放が確認できない場合は置換しない。
会議データや設定を初期化しない。稼働中の別アプリを終了しない。

2 回目以降は公開された feed を使い、アプリの「アップデートを確認…」から適用する。
更新に失敗した場合も、新版を直接コピーして成功扱いにしない。
同版の「最新です」は、新旧版間の Sparkle 適用成功と区別する。
更新前後の app の版・build、同梱 server の版、起動結果を記録して区別する。

失敗時は稼働版と診断ログを確認し、旧版が正常なら継続利用して Sparkle から再試行する。
初回起動失敗や server が起動せず更新操作もできない場合は、利用者の承認を得て復旧する。
対象 app/server の終了と port 解放を確認し、検証済みの退避旧版だけを復元する。
復元後も版・署名・起動・会議データ維持を確認し、手動復元を自動更新の成功とは記録しない。
安全な復元元がない場合は変更を止め、失敗した版・診断ログの場所・不足条件を利用者へ伝える。

OS の許可は利用者が確定する。録画の開始を更新確認に混ぜない。
旧 ad-hoc 開発版の権限が残る場合の整理も、対象権限を明示して確認を得てから行う。

## 範囲外

- pkg 形式、Finder 背景デザイン、新規グローバル依存、署名鍵の移行。
- 許可の自動付与、TCC の無断リセット、会議の自動開始。
- 録画権限判定そのものの変更、LLM・音声認識プロバイダーの追加。
