"""Missing setup must direct the owner to Finance, not a resend loop."""
import pytest

from gateway.there_business_context import BusinessEvidence


@pytest.mark.parametrize('field', ['company_binding', 'active_company_binding'])
@pytest.mark.parametrize('message,expected', [
    ('Today lunch 1200 JPY', 'select an active company, currency and timezone'),
    ('今日昼食1200円', '有効な会社・通貨・タイムゾーン'),
    ('今天午餐1200日元', '选择有效公司、币种和时区'),
])
def test_missing_company_has_actionable_setup_link(field, message, expected):
    evidence = BusinessEvidence()
    evidence.record({'status': 'needs_input', 'fields': [field], 'claims': [],
                     'url': 'https://untrusted.example.invalid/setup',
                     'request_key': 'PRIVATE_REQUEST_IDENTIFIER'})
    original = {'final_response': 'Finance draft created',
                'messages': [{'role': 'assistant', 'content': 'Finance draft created'}]}
    result = evidence.apply(original, message)
    assert expected in result['final_response']
    assert 'https://buildstudio-demo.com/#there-expenses' in result['final_response']
    assert result['messages'][-1]['content'] == result['final_response']
    assert 'PRIVATE_REQUEST_IDENTIFIER' not in result['final_response']
    assert 'untrusted.example' not in result['final_response']
    assert original['final_response'] == 'Finance draft created'


def test_missing_amount_keeps_complete_message_guidance():
    evidence = BusinessEvidence()
    evidence.record({'status': 'needs_input', 'fields': ['gross_amount'], 'claims': []})
    result = evidence.apply({}, 'Today lunch 1200 JPY')['final_response']
    assert 'Amount' in result and 'Resend one complete message' in result
    assert '#there-expenses' not in result


def test_manual_checkbox_keeps_manual_entry_guidance():
    evidence = BusinessEvidence()
    evidence.record({'status': 'needs_input', 'fields': ['manual_custom_fields'], 'claims': []})
    result = evidence.apply({}, 'Today lunch 1200 JPY')['final_response']
    assert 'manual entry' in result and 'resending this message cannot fill' in result
