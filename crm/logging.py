import logging
import re


class RedactRequestSecrets(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        message = re.sub(r'(/hooks/(?:zapi|omni)/[0-9a-f-]+/)[^/\s"]+', r'\1[redacted]', message)
        message = re.sub(r'(/integrations/meta/callback/)\?[^\s"]+', r'\1?[redacted]', message)
        record.msg, record.args = message, ()
        return True
