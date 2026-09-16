import re
with open('tests/test_review_audit.py', 'r') as f:
    content = f.read()

content = content.replace("assert fetched_res.health == StorageHealth.AVAILABLE\\n    fetched = fetched_res.record\\n    assert fetched is not None", "assert fetched_res.health == StorageHealth.AVAILABLE\n    fetched = fetched_res.record\n    assert fetched is not None")

content = content.replace("fetched = fetched_res.record\\n    assert fetched.execution_mode", "fetched = fetched_res.record\n    assert fetched.execution_mode")

content = content.replace("fetched = fetched_res.record\\n    assert fetched.lifecycle", "fetched = fetched_res.record\n    assert fetched.lifecycle")

content = content.replace("page1 = page1_res.records\\n    assert len(page1) == 3", "page1 = page1_res.records\n    assert len(page1) == 3")
content = content.replace("page1_again = page1_again_res.records\\n    assert len(page1_again) == 3", "page1_again = page1_again_res.records\n    assert len(page1_again) == 3")

content = content.replace("from auto_coder.review_audit import (\\n    StorageHealth,", "from auto_coder.review_audit import (\n    StorageHealth,")

with open('tests/test_review_audit.py', 'w') as f:
    f.write(content)
