工程は draft です。入力JSONの evidence だけを会議内の根拠として、議題・議論・決定・行動・確認事項を抽出してください。
すべての項目に evidence_id と、その根拠atom内に収まる絶対codepoint半開区間を付けてください。
背景は表記と文脈の補助にだけ使ってください。from と omissions は空配列にしてください。
次の閉じた形を厳守し、記載のないfieldとid fieldは返さないでください。
schema_versionは"ensemble-response-v1"です。
claimsは0〜32件で、各要素は{kind:"agenda"|"discussion"|"decision"|"action",text:非空文字列,evidence:[{evidence_id,char_start,char_end}],owner:文字列|null,due:文字列|null,from:[]}です。evidenceは1〜8件です。action以外のownerとdueはnullです。
questionsは0〜16件で、各要素は{kind:"conflict"|"missing_context",text:非空文字列,alternatives:[{text:非空文字列,evidence:[{evidence_id,char_start,char_end}]}],from:[]}です。conflictのalternativesは2〜4件、missing_contextは1〜4件です。
omissionsは空配列です。char_startとchar_endはboolではない整数で、char_startを含みchar_endを含みません。
入力JSON:
{{payload}}
