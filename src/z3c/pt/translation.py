from zope import component

from StringIO import StringIO

import generation
import clauses
import interfaces
import expressions
import itertools
import types
import utils
import config
import etree

class Node(object):
    """Element translation class.

    This class implements the translation node for an element in the
    template document tree.

    It's used internally by the translation machinery.
    """

    symbols = config.SYMBOLS
    
    def __init__(self, element):
        self.element = element

    @property
    def stream(self):
        return self.element.stream
    
    def update(self):
        self.element.update()
    
    def begin(self):
        self.stream.scope.append(set())
        self.stream.begin(self.serialize())
        
    def end(self):
        self.stream.end(self.serialize())
        self.stream.scope.pop()

    def body(self):
        if not self.skip:
            for element in self.element:
                element.node.update()

            for element in self.element:
                element.node.visit()
                    
    def visit(self, skip_macro=True):
        assert self.stream is not None, "Must use ``start`` method."

        if skip_macro and (self.method or self.define_macro):
            return

        for element in self.element:
            if not isinstance(element, Element):
                self.wrap_literal(element)

        self.update()
        self.begin()
        self.body()
        self.end()

    def serialize(self):
        """Serialize element into clause-statements."""

        _ = []

        # i18n domain
        if self.translation_domain is not None:
            _.append(clauses.Define(
                self.symbols.domain, types.value(repr(self.translation_domain))))

        # variable definitions
        if self.define is not None:
            for declaration, expression in self.define:
                if declaration.global_scope:
                    _.append(clauses.Define(
                        declaration, expression, self.symbols.scope))
                else:
                    _.append(clauses.Define(declaration, expression))

        # macro method
        for element in tuple(self.element):
            if not isinstance(element, Element):
                continue

            macro = element.node.method
            if macro is not None:
                # define macro
                subclauses = []
                subclauses.append(clauses.Method(
                    self.symbols.macro, macro.args))
                subclauses.append(clauses.Visit(element.node))
                _.append(clauses.Group(subclauses))
                
                # assign to variable
                _.append(clauses.Define(
                    macro.name, types.parts((types.value(self.symbols.macro),))))

        # tag tail (deferred)
        tail = self.element.tail
        if tail and not self.fill_slot:
            if isinstance(tail, unicode):
                tail = tail.encode('utf-8')
            _.append(clauses.Out(tail, defer=True))

        # condition
        if self.condition is not None:
            _.append(clauses.Condition(self.condition))

        # repeat
        if self.repeat is not None:
            variables, expression = self.repeat
            if len(variables) != 1:
                raise ValueError(
                    "Cannot unpack more than one variable in a "
                    "repeat statement.")
            _.append(clauses.Repeat(variables[0], expression))

        content = self.content

        # macro slot definition
        if self.define_slot:
            # check if slot has been filled
            variable = self.symbols.slot + self.define_slot
            if variable in itertools.chain(*self.stream.scope):
                content = types.value(variable)

        # set dynamic content flag
        dynamic = content or self.translate is not None

        # static attributes are at the bottom of the food chain
        attributes = self.static_attributes

        # dynamic attributes
        attrs = self.dynamic_attributes or ()
        dynamic_attributes = tuple(attrs)

        for variables, expression in attrs:
            if len(variables) != 1:
                raise ValueError("Tuple definitions in assignment clause "
                                     "is not supported.")

            variable = variables[0]
            attributes[variable] = expression

        # translated attributes
        translated_attributes = self.translated_attributes or ()
        for variable, msgid in translated_attributes:
            if msgid:
                if variable in dynamic_attributes:
                    raise ValueError(
                        "Message id not allowed in conjunction with "
                        "a dynamic attribute.")

                value = types.value('"%s"' % msgid)

                if variable in attributes:
                    default = '"%s"' % attributes[variable]
                    expression = _translate(value, default=default)
                else:
                    expression = _translate(value)
            else:
                if variable in dynamic_attributes or variable in attributes:
                    text = '"%s"' % attributes[variable]
                    expression = _translate(text)
                else:
                    raise ValueError("Must be either static or dynamic "
                                     "attribute when no message id "
                                     "is supplied.")

            attributes[variable] = expression

        # tag
        text = self.element.text
        if self.omit is not True:
            selfclosing = text is None and not dynamic and len(self.element) == 0
            tag = clauses.Tag(
                self.element.tag, attributes,
                expression=self.dict_attributes, selfclosing=selfclosing,
                cdata=self.cdata is not None)
            if self.omit:
                _.append(clauses.Condition(
                    _not(self.omit), [tag], finalize=False))
            else:
                _.append(tag)

        # tag text (if we're not replacing tag body)
        if text and not dynamic:
            if isinstance(text, unicode):
                text = text.encode('utf-8')
            _.append(clauses.Out(text))

        # dynamic content
        if content:
            msgid = self.translate
            if msgid is not None:
                if msgid:
                    raise ValueError(
                        "Can't use message id with dynamic content translation.")
                
                _.append(clauses.Translate())
            _.append(clauses.Write(content))

        # use macro
        elif self.use_macro:
            # for each fill-slot element, create a new output stream
            # and save value in a temporary variable
            kwargs = []
            for element in self.element.xpath(
                './/*[@metal:fill-slot]', namespaces={'metal': config.METAL_NS}):
                variable = self.symbols.slot+element.node.fill_slot
                kwargs.append((variable, variable))
                
                subclauses = []
                subclauses.append(clauses.Define(
                    types.declaration((self.symbols.out, self.symbols.write)),
                    types.template('%(generation)s.initialize_stream()')))
                subclauses.append(clauses.Visit(element.node))
                subclauses.append(clauses.Assign(
                    types.template('%(out)s.getvalue()'), variable))
                _.append(clauses.Group(subclauses))
                
            _.append(clauses.Assign(self.use_macro, self.symbols.metal))

            # compute macro function arguments and create argument string
            arguments = ", ".join(
                tuple("%s=%s" % (arg, arg) for arg in \
                      itertools.chain(*self.stream.scope))+
                tuple("%s=%s" % kwarg for kwarg in kwargs))
                
            _.append(clauses.Write(
                types.value("%s(%s)" % (self.symbols.metal, arguments))))

        # translate body
        elif self.translate is not None:
            msgid = self.translate
            if not msgid:
                msgid = self.create_msgid()

            # for each named block, create a new output stream
            # and use the value in the translation mapping dict
            elements = [e for e in self.element if e.node.translation_name]

            if elements:
                mapping = self.symbols.mapping
                _.append(clauses.Assign(types.value('{}'), mapping))
            else:
                mapping = 'None'

            for element in elements:
                name = element.node.translation_name

                subclauses = []
                subclauses.append(clauses.Define(
                    types.declaration((self.symbols.out, self.symbols.write)),
                    types.template('%(generation)s.initialize_stream()')))
                subclauses.append(clauses.Visit(element.node))
                subclauses.append(clauses.Assign(
                    types.template('%(out)s.getvalue()'),
                    "%s['%s']" % (mapping, name)))

                _.append(clauses.Group(subclauses))

            _.append(clauses.Assign(
                _translate(types.value(repr(msgid)), mapping=mapping,
                           default=self.symbols.marker), self.symbols.result))

            # write translation to output if successful, otherwise
            # fallback to default rendition; 
            result = types.value(self.symbols.result)
            condition = types.template('%(result)s is not %(marker)s')
            _.append(clauses.Condition(condition,
                        [clauses.UnicodeWrite(result)]))

            subclauses = []
            if self.element.text:
                subclauses.append(clauses.Out(self.element.text.encode('utf-8')))
            for element in self.element:
                name = element.node.translation_name
                if name:
                    value = types.value("%s['%s']" % (mapping, name))
                    subclauses.append(clauses.Write(value))
                else:
                    subclauses.append(clauses.Out(element.tostring()))
            if subclauses:
                _.append(clauses.Else(subclauses))

        return _

    def wrap_literal(self, element):
        index = self.element.index(element)

        t = self.element.makeelement(utils.meta_attr('literal'))
        t.attrib[utils.meta_attr('omit-tag')] = ''
        t.tail = element.tail
        t.text = unicode(element)
        for child in element.getchildren():
            t.append(child)
        self.element.remove(element)
        self.element.insert(index, t)
        t.update()

    def create_msgid(self):
        """Create an i18n msgid from the tag contents."""

        out = StringIO(self.element.text)
        for element in self.element:
            name = element.node.translation_name
            if name:
                out.write("${%s}" % name)
                out.write(element.tail)
            else:
                out.write(element.tostring())

        msgid = out.getvalue().strip()
        msgid = msgid.replace('  ', ' ').replace('\n', '')
        
        return msgid

class Element(etree.ElementBase):
    """Template element class.

    To start translation at this element, use the ``start`` method,
    providing a code stream object.
    """

    node = property(Node)
    
    def start(self, stream):
        self._stream = stream
        self.node.visit()

    @property
    def stream(self):
        while self is not None:
            try:
                return self._stream
            except AttributeError:
                self = self.getparent()

        raise ValueError("Can't locate stream object.")

    meta_cdata = utils.attribute(
        utils.meta_attr('cdata'))
    
    meta_omit = utils.attribute(
        utils.meta_attr('omit-tag'))

    meta_attributes =  utils.attribute(
        utils.meta_attr('attributes'), lambda p: p.definitions)

    meta_replace = utils.attribute(
        utils.meta_attr('replace'), lambda p: p.output)

class VariableInterpolation:
    def update(self):
        translator = self.translator
        
        if self.text is not None:
            while self.text:
                text = self.text
                m = translator.interpolate(text)
                if m is None:
                    break

                t = self.makeelement(utils.meta_attr('interpolation'))
                expression = "structure " + \
                             (m.group('expression') or m.group('variable'))
                t.attrib[utils.meta_attr('replace')] = expression
                t.tail = text[m.end():]
                self.insert(0, t)
                t.update()

                if m.start() == 0:
                    self.text = text[2-len(m.group('prefix')):m.start()+1]
                else:
                    self.text = text[:m.start()+1]

        if self.tail is not None:
            while self.tail:
                m = translator.interpolate(self.tail)
                if m is None:
                    break

                t = self.makeelement(utils.meta_attr('interpolation'))
                expression = "structure " + \
                             (m.group('expression') or m.group('variable'))
                t.attrib[utils.meta_attr('replace')] = expression
                t.tail = self.tail[m.end():]
                parent = self.getparent()
                parent.insert(parent.index(self)+1, t)
                t.update()
                                
                self.tail = self.tail[:m.start()+len(m.group('prefix'))-1]

        for name in utils.get_attributes_from_namespace(self, config.XHTML_NS):
            value = self.attrib[name]

            if translator.interpolate(value):
                del self.attrib[name]

                attributes = utils.meta_attr('attributes')
                expr = '%s string: %s' % (name, value)
                if attributes in self.attrib:
                    self.attrib[attributes] += '; %s' % expr
                else:
                    self.attrib[attributes] = expr

def translate_xml(body, parser, *args, **kwargs):
    root, doctype = parser.parse(body)
    return translate_etree(root, doctype=doctype, *args, **kwargs)

def translate_etree(root, macro=None, doctype=None,
                    params=[], default_expression='python'):
    if not isinstance(root, Element):
        raise ValueError("Must define valid namespace for tag: '%s.'" % root.tag)

    # skip to macro
    if macro is not None:
        elements = root.xpath(
            'descendant-or-self::*[@metal:define-macro="%s"]' % macro,
            namespaces={'metal': config.METAL_NS})

        if not elements:
            raise ValueError("Macro not found: %s." % macro)

        root = elements[0]
        del root.attrib[utils.metal_attr('define-macro')]
        
    # set default expression name
    if utils.get_namespace(root) == config.TAL_NS:
        tag = 'default-expression'
    else:
        tag = utils.tal_attr('default-expression')

    if not root.attrib.get(tag):
        root.attrib[tag] = default_expression

    # set up code generation stream
    if macro is not None:
        wrapper = generation.macro_wrapper
    else:
        wrapper = generation.template_wrapper

    # initialize code stream object
    stream = generation.CodeIO(
        root.node.symbols, indentation=1, indentation_string="\t")

    # initialize variable scope
    stream.scope.append(set(
        (stream.symbols.out, stream.symbols.write, stream.symbols.scope) + \
        tuple(params)))

    # output doctype if any
    if doctype and isinstance(doctype, (str, unicode)):
        dt = (doctype +'\n').encode('utf-8')
        doctype = clauses.Out(dt)
        stream.scope.append(set())
        stream.begin([doctype])
        stream.end([doctype])
        stream.scope.pop()

    # start generation
    root.start(stream)

    extra = ''

    # prepare args
    args = ', '.join(params)
    if args:
        args += ', '

    # prepare kwargs
    kwargs = ', '.join("%s=None" % param for param in params)
    if kwargs:
        kwargs += ', '

    # prepare selectors
    for selector in stream.selectors:
        extra += '%s=None, ' % selector

    # we need to ensure we have _context for the i18n handling in
    # the arguments. the default template implementations pass
    # this in explicitly.
    if stream.symbols.context not in params:
        extra += '%s=None, ' % stream.symbols.context

    code = stream.getvalue()

    class generator(object):
        @property
        def stream(self):
            return stream
        
        def __call__(self):
            parameters = dict(
                args=args, kwargs=kwargs, extra=extra, code=code)
            parameters.update(stream.symbols.__dict__)

            return wrapper % parameters, {stream.symbols.generation: generation}

    return generator()

def translate_text(body, parser, *args, **kwargs):
    root, doctype = parser.parse("<html xmlns='%s'></html>" % config.XHTML_NS)
    root.text = body
    root.attrib[utils.meta_attr('omit-tag')] = ''
    return translate_etree(root, doctype=doctype, *args, **kwargs)
    
def _translate(value, mapping=None, default=None):
    format = "_translate(%s, domain=%%(domain)s, mapping=%s, context=%%(context)s, " \
             "target_language=%%(language)s, default=%s)"
    return types.template(
        format % (value, mapping, default))

def _not(value):
    return types.value("not (%s)" % value)
