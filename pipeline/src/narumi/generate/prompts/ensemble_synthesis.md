工程は synthesis です。候補数を真実性の根拠にせず、evidence と候補を照合して統合してください。
候補間の不一致は確認事項に残してください。直接入力された全claim IDを、出力の from または omissions.from のどちらかで扱ってください。
すべての根拠範囲は入力JSONの evidence 内に限定してください。
次の閉じた形を厳守し、記載のないfieldとid fieldは返さないでください。
schema_versionは"ensemble-response-v1"です。
claimsは0〜32件で、各要素は{kind:"agenda"|"discussion"|"decision"|"action",text:非空文字列,evidence:[{evidence_id,char_start,char_end}],owner:文字列|null,due:文字列|null,from:[直接入力claim ID]}です。evidenceは1〜8件です。action以外のownerとdueはnullです。
questionsは0〜16件で、各要素は{kind:"conflict"|"missing_context",text:非空文字列,alternatives:[{text:非空文字列,evidence:[{evidence_id,char_start,char_end}]}],from:[直接入力claim ID]}です。conflictのalternativesは2〜4件、missing_contextは1〜4件です。
omissionsは0〜32件で、各要素は{from:直接入力claim ID,reason:"duplicate"|"not_selected"}です。char_startとchar_endはboolではない整数で、char_startを含みchar_endを含みません。
入力JSON:
{{payload}}
