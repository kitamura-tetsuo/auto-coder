import re
with open("tests/test_dashboard_reviews.py", "r") as f:
    c = f.read()

c = re.sub(r'import json.*?$', '', c, flags=re.DOTALL)

with open("tests/test_dashboard_reviews.py", "w") as f:
    f.write(c)
