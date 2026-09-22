import hashlib
import json
from pathlib import Path
from personal_agent.api.request_payload import validate_reply_context


def test_shared_ios_vector_exact_utf8_document_binding():
    vector=json.loads((Path(__file__).parents[2]/'tests/fixtures/dal_timeline.synthetic.json').read_text())
    assert vector['synthetic'] is True
    document=vector['document'];raw=document['text'].encode()
    assert hashlib.sha256(raw).hexdigest()==document['body_sha256']
    assert len(raw)==document['total_bytes'] and document['complete'] and document['next_offset'] is None
    assert validate_reply_context(vector['reply_context'])==vector['reply_context']
    assert vector['update']['artifact']['artifact_id']==document['artifact_id']
