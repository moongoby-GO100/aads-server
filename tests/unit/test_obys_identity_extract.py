"""신분증 판독과 입사서류 프로필 회귀 검증."""
import json
import os
from io import BytesIO

import pytest
from fastapi import UploadFile, HTTPException

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")
from app.services import obys_identity_extract as identity
from app.services import yeoljeong_finance_service as svc
from tests.unit.test_obys_bankbook_extract import hr, OWNER, EMP, _png, _scripted_ocr


@pytest.mark.parametrize('digit,year', [('1',1990),('2',1990),('5',1990),('6',1990),('3',2090),('4',2090),('7',2090),('8',2090)])
def test_birth_century(digit, year):
    assert identity.parse_identity_text(f'900101-{digit}234567', 'id_card')['birth_date'] == f'{year}-01-01'


def test_register_selects_employee_line_only():
    text = '세대주 박영희 800101-2234567\n자녀 김민우 010203-3234567\n주소 서울특별시 성북구 동소문로 20'
    assert identity.parse_identity_text(text, 'resident_register', '김민우')['birth_date'] == '2001-02-03'
    assert not identity.parse_identity_text(text, 'resident_register', '김민')['birth_date']
    assert not identity.parse_identity_text(text, 'resident_register', '')['birth_date']


@pytest.mark.asyncio
async def test_rrn_suffix_never_in_result_record_or_logs(caplog):
    text = '김민우 900101-1234567\n주소 서울특별시 성북구 동소문로 20 900101-1234567'
    result = await identity.extract_identity_bounded(_png(), document_type='id_card', ocr=_scripted_ocr({'text':text,'confidence':0.9}))
    record = {}
    identity.apply_to_record(record, result)
    assert result['extract_status'] == 'extracted'
    dump = json.dumps([result,record],ensure_ascii=False) + caplog.text
    assert '1234567' not in dump and '900101' not in dump and text not in dump


@pytest.mark.asyncio
async def test_unreadable_and_low_confidence_need_review():
    for text, confidence in [('',0),('900101-1234567\n서울특별시 성북구 동소문로 20',0.1)]:
        result = await identity.extract_identity_bounded(_png(), document_type='id_card', ocr=_scripted_ocr({'text':text,'confidence':confidence}))
        assert result['extract_status'] == 'needs_review'
        assert not result['birth_date'] and not result['address']


@pytest.mark.asyncio
async def test_rotation_retry_and_timeout(monkeypatch):
    ocr = _scripted_ocr({'text':'','confidence':0}, {'text':'900101-1234567\n서울특별시 성북구 동소문로 20','confidence':0.9})
    result = await identity.extract_identity_bounded(_png(), document_type='id_card', ocr=ocr)
    assert result['extract_rotation'] == 180 and result['extract_attempts'] == 2
    import asyncio
    async def slow(data):
        await asyncio.sleep(1)
    monkeypatch.setenv('OBYS_IDENTITY_OCR_TIMEOUT_SEC','0.01')
    result = await identity.extract_identity_bounded(_png(), document_type='id_card', ocr=slow)
    assert result['extract_status'] == 'needs_review'


async def upload():
    return await svc.save_onboarding_document(employee_name='김민우', employee_email=EMP, branch='', document_type='id_card', issue_date='', memo='', user=OWNER, upload=UploadFile(filename='id.png',file=BytesIO(_png())))


@pytest.mark.asyncio
async def test_upload_failure_not_propagated(hr, monkeypatch):  # noqa: F811 (pytest 픽스처 hr 를 import 해 인자로 받는다)
    monkeypatch.setattr(identity.bb, '_default_ocr', _scripted_ocr(RuntimeError('private OCR failure')))
    document = await upload()
    assert document['extract_status'] == 'needs_review'
    assert document['extracted_fields'] == {}


@pytest.mark.asyncio
async def test_profile_merge_and_direct_input_not_overwritten(hr, monkeypatch):  # noqa: F811 (pytest 픽스처 hr 를 import 해 인자로 받는다)
    monkeypatch.setattr(identity.bb, '_default_ocr', _scripted_ocr({'text':'900101-1234567\n서울특별시 성북구 동소문로 20','confidence':0.9}))
    document = await upload()
    profile = svc._employee_onboarding_profile(svc._read_hr('onboarding_documents',OWNER), employee_email=EMP, employee_request_id='join-emp-mia')
    assert profile['birth_date'] == '1990-01-01'
    assert profile['address'] == '서울특별시 성북구 동소문로 20'
    rows = svc._read_file_rows('employee_join_requests')
    rows[0].update(birth_date='1985-02-03', address='직접 입력 주소')
    svc._write_file_rows('employee_join_requests',rows)
    employee = svc.list_approved_employees(OWNER)[0]
    assert employee['birth_date'] == '1985-02-03' and employee['address'] == '직접 입력 주소'
    member = {**OWNER, 'current_membership':{**OWNER['current_membership'],'role':'member'}}
    with pytest.raises(HTTPException) as denied:
        await svc.reextract_identity_document(document['id'],member)
    assert denied.value.status_code == 403
    await upload()
    with pytest.raises(HTTPException) as old:
        await svc.reextract_identity_document(document['id'],OWNER)
    assert old.value.status_code == 409


def test_ocr_misread_special_city_is_seoul():
    # 운영 서류 14건 실측(2026-11-06): OCR 이 "특별시"는 읽었지만 앞의 "서울"을 다른 글자로 읽어 주소 0건이었다.
    text = '성명 김민우\n서올특별시 성북구 동소문로 20'
    assert identity.parse_identity_text(text, 'id_card')['address'] == '서울특별시 성북구 동소문로 20'
    assert identity.parse_identity_text('경기도 고양시 덕양구 화정로 1', 'id_card')['address'] == '경기도 고양시 덕양구 화정로 1'


def test_register_number_on_next_line_only_when_no_other_person():
    split = '본인 김민우\n010203-3234567\n주소 서울특별시 성북구 동소문로 20'
    assert identity.parse_identity_text(split, 'resident_register', '김민우')['birth_date'] == '2001-02-03'
    # 다음 줄이 다른 세대원이면 그 번호를 쓰지 않는다
    other = '자녀 김민우\n배우자 박영희 800101-2234567'
    assert not identity.parse_identity_text(other, 'resident_register', '김민우')['birth_date']
    # 이름 줄에 이미 번호가 있으면 다음 줄을 보지 않는다
    own = '김민우 010203-3234567\n800101-2234567'
    assert identity.parse_identity_text(own, 'resident_register', '김민우')['birth_date'] == '2001-02-03'


@pytest.mark.asyncio
async def test_partial_fields_kept_when_confident_but_not_marked_extracted():
    text = '김민우 900101-1234567\n주소 판독 불가'
    result = await identity.extract_identity_bounded(_png(), document_type='id_card', ocr=_scripted_ocr({'text': text, 'confidence': 0.9}))
    assert result['extract_status'] == 'needs_review'
    assert result['birth_date'] == '1990-01-01' and not result['address']
    record = {}
    identity.apply_to_record(record, result)
    assert record['extracted_fields'] == {'birth_date': '1990-01-01'}
    low = await identity.extract_identity_bounded(_png(), document_type='id_card', ocr=_scripted_ocr({'text': text, 'confidence': 0.2}))
    assert not low['birth_date']
