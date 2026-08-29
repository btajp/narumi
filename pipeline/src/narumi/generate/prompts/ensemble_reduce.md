工程は reduce です。直接入力された主張を、再展開された evidence と照合しながら統合・縮約してください。
直接入力された全claim IDを、出力の from または omissions.from のどちらかで扱ってください。
既知の確認事項を解決済みとみなして削除しないでください。根拠のない補完や引用範囲の拡張をしないでください。
次の閉じた形を厳守し、記載のないfieldとid fieldは返さないでください。
schema_versionは"ensemble-response-v1"です。
claimsは0〜32件で、各要素は{kind:"agenda"|"discussion"|"decision"|"action",text:非空文字列,evidence:[{evidence_id,char_start,char_end}],owner:文字列|null,due:文字列|null,from:[直接入力claim ID]}です。evidenceは1〜8件です。action以外のownerとdueはnullです。
questionsは0〜16件で、各要素は{kind:"conflict"|"missing_context",text:非空文字列,alternatives:[{text:非空文字列,evidence:[{evidence_id,char_start,char_end}]}],from:[直接入力claim ID]}です。conflictのalternativesは2〜4件、missing_contextは1〜4件です。
omissionsは0〜32件で、各要素は{from:直接入力claim ID,reason:"duplicate"|"not_selected"}です。char_startとchar_endはboolではない整数で、char_startを含みchar_endを含みません。
入力JSON:
{{payload}}
