from inspect import isclass
from sys import maxint

from core.service import CoreService


class Startup(CoreService):
    """
    A CORE service to start other services in order, serially
    """
    _name = 'startup'
    _group = 'Utility'
    _depends = ()
    _dirs = ()
    _configs = ('startup.sh',)
    _startindex = maxint
    _startup = ('sh startup.sh',)
    _shutdown = ()
    _validate = ()

    @staticmethod
    def is_startup_service(s):
        return isinstance(s, Startup) or (isclass(s) and issubclass(s, Startup))

    @classmethod
    def generateconfig(cls, node, filename, services):
        if filename != cls._configs[0]:
            return ''
        script = '#!/bin/sh\n' \
                 '# auto-generated by Startup (startup.py)\n\n' \
                 'exec > startup.log 2>&1\n\n'
        for s in sorted(services, key=lambda x: x._startindex):
            if cls.is_startup_service(s) or len(str(s._starttime)) > 0:
                continue
            start = '\n'.join(s.getstartup(node, services))
            if start:
                script += start + '\n'
        return script
