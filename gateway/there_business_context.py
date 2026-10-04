"""Request-local business proof and actual receipts; no model-provided authority."""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import re
import threading

_proof = ContextVar('there_business_proof',default='')
_evidence = ContextVar('there_business_evidence',default=None)


def current_business_context(): return _proof.get()


def record_business_result(response):
    evidence=_evidence.get()
    if evidence is not None: evidence.record(response)


def expense_requested(message):
    return bool(isinstance(message,str) and not re.search(r'翻译|翻譯|翻訳|translate|总结|總結|要約|summari[sz]e|引用|quoted?\b|预算|預算|budget|价格|價格|price\b|多少钱|いくら|how much|不要登记|先别登记|do not record|don[’\']t record',message,re.I)
        and re.search(r'\d',message)
        and re.search(r'日元|円|人民币|JPY|CNY|USD|expense|开销|支出|経費|午餐|午饭|晚餐|电车|咖啡|交通|办公|昼食|夕食|電車|コーヒー|lunch|dinner|train|coffee|taxi|hotel|there_finance_expenses',message,re.I))


class BusinessEvidence:
    def __init__(self): self.records,self.lock=[],threading.Lock()
    def record(self,value):
        with self.lock: self.records.append(copy.deepcopy(value))
    def has_records(self):
        with self.lock: return bool(self.records)

    def apply(self,result,message):
        with self.lock: records=copy.deepcopy(self.records)
        claimed=bool(re.search(r'(?:Finance|expense|draft).{0,60}(?:created|recorded|saved)|(?:已|成功).{0,10}(?:登记|记录|创建).{0,30}(?:Finance|开销|费用|草稿)',str(result.get('final_response','')),re.I))
        if not records and not expense_requested(message) and not claimed: return result
        if re.search(r'[\u3040-\u30ff]|円|昼食|夕食|本日|経費|電車',message):
            language='ja'
            label='Finance の現在の記録です。今回、正式な計上・支払・メール送信は行っていません。'
            missing='今回の経費登録の実行記録はありません。登録は未確認です。必要な情報を補ってください。'
            needs='不足している項目: {fields}。この回では登録していません。例:「今日昼食1200円、電車460円」のように、経費の全文をもう一度送ってください。'
            company='会社'; document='経費'; deleted='削除済み'; uncertain='登録結果は未確認です。再作成せず、同じメッセージの結果を確認してください。'
        elif re.search(r'[\u3400-\u9fff]',message):
            language='zh'
            label='以下是 Finance 当前记录。本轮未正式入账、支付或发信。'
            missing='本轮没有开销登记执行回执，登记尚未确认。请补充必要信息。'
            needs='缺少或需明确的字段：{fields}。本轮未登记。请发一条完整消息，例如“今天午餐1200日元，电车460日元”，并补齐列出的字段。'
            company='公司'; document='开销单'; deleted='已删除'; uncertain='登记结果尚未确认。请查询同一条消息的结果，避免重新创建。'
        else:
            language='en'
            label='Current Finance records. No posting, payment or mail was performed in this turn.'
            missing='No expense registration receipt was recorded. Registration is unconfirmed; supply the missing information.'
            needs='Missing or unclear fields: {fields}. Nothing was registered. Resend one complete message, for example “Today lunch 1200 JPY, train 460 JPY”, including the listed fields.'
            company='Company'; document='Expense'; deleted='Deleted'; uncertain='Registration is unconfirmed. Check the same message’s receipt before creating anything again.'
        # Only business-facing native values enter the final answer. Routing
        # UUIDs, request hashes, grants and internal error codes remain private.
        claims={}; companies={}
        for row in records:
            if row.get('status')!='completed': continue
            for claim in row.get('claims',[]):
                if isinstance(claim,dict) and type(claim.get('id')) is int:
                    claims[claim['id']]=claim
                    companies[claim['id']]=row.get('company_name','')
        status_names={
            'DRAFT':('Draft, unsubmitted','未提出の下書き','未提交草稿'),
            'SUBMITTED':('Submitted','提出済み','已提交'),'REVIEWED':('Reviewed','確認済み','已审核'),
            'APPROVED':('Approved','承認済み','已批准'),'REJECTED':('Rejected','却下','已拒绝'),
            'PAID':('Paid','支払済み','已支付'),'DELETED':('Deleted','削除済み','已删除')}
        slot={'en':0,'ja':1,'zh':2}[language]
        lines=[]
        for ident,claim in claims.items():
            native_company=safe_text(companies[ident])
            if claim.get('status')=='DELETED':
                lines.append(f'- {company}: {native_company} · {document} #{ident}: {deleted}')
            else:
                status=status_names.get(claim.get('status'),('Unconfirmed','未確認','未确认'))[slot]
                fields=' · '.join(safe_text(claim.get(k,'')) for k in ('expense_date','title'))
                amount=safe_text(claim.get('gross_amount',''))+' '+safe_text(claim.get('currency',''))
                lines.append(f'- {company}: {native_company} · {fields} · {amount} · {status} · {document} #{ident}')
        rendered=label+'\n\n'+'\n'.join(lines) if lines else missing
        needed=[field for row in records if row.get('status')=='needs_input' for field in row.get('fields',[])]
        if needed:
            labels=field_labels(needed,records,language)
            if {'company_binding', 'active_company_binding'} & set(needed):
                setup_url='https://buildstudio-demo.com/#there-expenses'
                setup={
                    'en': 'Finance expense setup is incomplete or the selected company is unavailable. Nothing was registered. Open [Finance expense setup]({url}), select an active company, currency and timezone, and enable expense registration. Then resend one complete expense message. The company cannot be selected in chat.',
                    'ja': 'Finance の経費設定が未完了、または選択した会社が利用できません。今回は登録していません。[Finance の経費設定]({url})で有効な会社・通貨・タイムゾーンを選び、経費登録を有効にしてください。その後、経費の全文をもう一度送ってください。チャット内では会社を選択できません。',
                    'zh': 'Finance 费用设置尚未完成，或所选公司已不可用。本轮未登记。请打开 [Finance 费用设置]({url})，选择有效公司、币种和时区，并启用费用登记，再发送一条完整的开销消息。不能通过聊天代选公司。',
                }[language]
                rendered=(rendered+'\n\n' if lines else '')+setup.format(url=setup_url)
            elif 'manual_custom_fields' in needed:
                manual={'en':'A required Finance checkbox needs manual entry in Finance. Nothing was registered automatically; resending this message cannot fill that field.',
                    'ja':'Finance のチェックボックス項目は Finance で手動入力が必要です。自動登録は行っていません。このメッセージの再送では入力できません。',
                    'zh':'Finance 的复选框字段需要在 Finance 手工填写。本轮未自动登记，重新发送消息也无法代填该字段。'}[language]
                rendered=(rendered+'\n\n' if lines else '')+manual
            else: rendered=(rendered+'\n\n' if lines else '')+needs.format(fields=', '.join(labels))
        elif not lines or any(row.get('status') in {'unconfirmed','rejected','not_recorded'} for row in records):
            rendered=(rendered+'\n\n' if lines else '')+uncertain
        result=dict(result,final_response=rendered)
        messages=result.get('messages')
        if isinstance(messages,list) and messages and isinstance(messages[-1],dict) and messages[-1].get('role')=='assistant' and not messages[-1].get('tool_calls'):
            result['messages']=[*messages[:-1],{**messages[-1],'content':rendered}]
        return result


def safe_text(value):
    value=' '.join(str(value).split())[:500].replace('MEDIA:','MEDIA\\:')
    return re.sub(r'([\\`*_\[\]<>])',r'\\\1',value)


def field_labels(fields,records,language):
    names={
        'title':('Expense purpose','用途','用途'),'source_quote':('Expense details','経費の詳細','开销详情'),
        'gross_amount':('Amount','金額','金额'),'expense_date':('Date','日付','日期'),
        'currency':('Currency','通貨','币种'),'company_binding':('Finance company setup','Finance の会社設定','Finance 公司设置'),
        'registration_intent':('Permission to record','登録する意思','是否授权登记'),
        'expense_intent':('Expense or refund','支出か返金か','支出还是退款'),
        'complete_expense_batch':('Complete expense list','経費リスト全体','完整开销清单'),
        'tax_review':('Tax details','税務情報','税务信息'),'category':('Category','分類','分类'),
        'memo':('Memo','メモ','备注'),'required_fields':('Required information','必須情報','必填信息')}
    slot={'en':0,'ja':1,'zh':2}[language]; labels=[]
    custom={field.get('key'):field.get('label') for row in records for field in row.get('required_fields',[])
            if isinstance(field,dict) and isinstance(field.get('label'),str)}
    for field in fields:
        if not isinstance(field,str): continue
        match=re.fullmatch(r'items\.([0-9]+)\.(.+)',field)
        key=match[2] if match else field
        if key.startswith('extra.'):
            label=safe_text(custom.get(key[6:],names['required_fields'][slot]))
        else: label=names.get(key,names['required_fields'])[slot]
        if match: label=({'en':'Expense ','ja':'経費 ','zh':'第 '}[language]+match[1]+('项：' if language=='zh' else ': ')+label)
        if label not in labels: labels.append(label)
    return labels or [names['required_fields'][slot]]


@contextmanager
def business_context_scope(proof):
    evidence=BusinessEvidence()
    a,b=_proof.set(proof),_evidence.set(evidence)
    try: yield evidence
    finally: _proof.reset(a); _evidence.reset(b)
