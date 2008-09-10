import errno
import os.path
from z3c.pt.pagetemplate import PageTemplateFile
from z3c.pt.texttemplate import TextTemplateFile

def cache(func):
    def load(self, *args):
        template = self.registry.get(args)
        if template is None:
            self.registry[args] = template = func(self, *args)
        return template
    return load
    
class TemplateLoader(object):
    """Template loader tool. To load templates using relative
    filenames, pass a sequence of paths (or a single path) as
    ``search_path``; if ``auto_reload`` is set, templates will be
    reloaded when modified."""

    def __init__(self, search_path=None, auto_reload=False, parser=None):
        if search_path is None:
            search_path = []
        if isinstance(search_path, basestring):
            search_path = [search_path]
        self.search_path = search_path
        self.auto_reload = auto_reload
        self.parser = parser
        self.registry = {}

    @cache
    def _load(self, filename, klass):
        if os.path.isabs(filename):
            return klass(filename, self.parser)

        for path in self.search_path:
            path = os.path.join(path, filename)
            try:
                return klass(
                    path, self.parser, auto_reload=self.auto_reload)
            except OSError, e:
                if e.errno != errno.ENOENT:
                    raise

        raise ValueError("Can not find template %s" % filename)

    def load_page(self, filename):
        return self._load(filename, PageTemplateFile)

    def load_text(self, filename):
        return self._load(filename, TextTemplateFile)

