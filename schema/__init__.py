# TODO schema.check doesnt support switching between arg and kwarg at call time.
# u have to use which ever way you defined the annotation. ie default value?
# or actually is this a feature? helpful constraint?

import functools
import inspect
import pprint
import re
import util.async
import util.data
import util.dicts
import util.exceptions
import util.func
import util.strings
import sys
import traceback
import types


disabled = False


_schema_commands = (':U', # union
                    ':I', # intersection
                    ':O', # optional
                    ':fn')


json = (':U',
        list,
        str,
        dict,
        int,
        float,
        tuple,
        bool,
        type(None))


def is_valid(schema, value):
    try:
        _validate(schema, value)
        return True
    except AssertionError:
        return False


def validate(schema, value, exact_match=False):
    """
    >>> import pytest

    ### basic usage

    # simple values represent themselves
    >>> schema = int
    >>> assert validate(schema, 123) == 123
    >>> with pytest.raises(AssertionError):
    ...     validate(schema, '123')

    # lists represent variable length homogenous lists/tuples
    >>> schema = [int]
    >>> assert validate(schema, [1, 2]) == [1, 2]
    >>> with pytest.raises(AssertionError):
    ...     validate(schema, [1, '2'])

    # tuples represent fixed length heterogenous lists/tuples
    >>> schema = (int, int)
    >>> assert validate(schema, [1, 2]) == [1, 2]
    >>> with pytest.raises(AssertionError):
    ...     validate(schema, [1])

    ### union types with :U
    >>> schema = (':U', int, float)
    >>> assert validate(schema, 1) == 1
    >>> assert validate(schema, 1.0) == 1.0
    >>> with pytest.raises(AssertionError):
    ...     validate(schema, '1')

    ### dicts can use types and values for k's and v's, and also lambdas for v'util.

    # dicts with types->types
    >>> schema = {str: int}
    >>> assert validate(schema, {'1': 2}) == {'1': 2}
    >>> with pytest.raises(AssertionError):
    ...     validate(schema, {'1': 2.0})

    # dicts with types->values. fyi, the only type allowed for keys is "str".
    >>> schema = {str: 'bob'}
    >>> assert validate(schema, {'alias': 'bob'}) == {'alias': 'bob'}
    >>> with pytest.raises(AssertionError):
    ...     validate(schema, {'alias': 'joe'})


    # dicts with values->types
    >>> schema = {'name': float}
    >>> assert validate(schema, {'name': 3.14}) == {'name': 3.14}
    >>> with pytest.raises(AssertionError):
    ...     validate(schema, {'name': 314})

    # dicts with complex validation
    >>> assert validate({'name': lambda x: x in ['john', 'jane']}, {'name': 'jane'}) == {'name': 'jane'}
    >>> with pytest.raises(AssertionError):
    ...     validate({'name': lambda x: x in ['john', 'jane']}, {'name': 'rose'})

    # dicts with :O k's provide a value for a missing key and validate provided keys
    >>> schema = {'name': (':O', str, 'jane')}
    >>> assert validate(schema, {}) == {'name': 'jane'}
    >>> assert validate(schema, {'name': 'rose'}) == {'name': 'rose'}
    >>> with pytest.raises(AssertionError):
    ...     validate(schema, {'name': 123})

    # dicts with only type keys can be empty
    >>> schema = {str: str}
    >>> assert validate(schema, {}) == {}

    # validate is recursive, so nest schemas freely
    >>> schema = {'users': [{'name': (str, str), 'id': int}]}
    >>> obj = {'users': [{'name': ['jane', 'smith'], 'id': 85},
    ...                  {'name': ['john', 'smith'], 'id': 93}]}
    >>> assert validate(schema, obj) == obj
    >>> with pytest.raises(AssertionError):
    ...     validate(schema, {'users': [{'name': ('jane', 'e', 'smith'), 'id': 85}]})

    ### schema based pattern matching

    # # with a combination of values and object, we can express complex assertions on data
    # while True:
    #     msg = socket.recv()
    #     if validate([":order", {'sender': str, 'instructions': [str]], msg):
    #         key, val = msg
    #         run_order(val)
    #     elif validate([":shutdown", object]):
    #         sys.exit(1)
    #     else:
    #         print('unknown message')
    #
    """
    if disabled:
        return value
    return _validate(schema, value, exact_match)


def _validate(schema, value, exact_match=False):
    with util.exceptions.update(_updater(schema, value), AssertionError):
        # TODO does this block belong in _check()? should validate and _check even be seperate?
        value_is_a_future = util.async.is_future(value)
        schema_is_a_future_type = util.async.is_future(schema) and type(schema) is type
        if value_is_a_future and not schema_is_a_future_type:
            future = type(value)()
            @value.add_done_callback
            def fn(f):
                try:
                    future.set_result(_validate(schema, f.result()))
                except Exception as e:
                    future.set_exception(e)
            return future
        elif isinstance(schema, dict):
            assert isinstance(value, dict), 'value {} <{}> should be a dict for schema: {} <{}>'.format(value, type(value), schema, type(schema))
            value, validated_schema_items = _check_for_items_in_value_that_dont_satisfy_schema(schema, value, exact_match)
            return _check_for_items_in_schema_missing_in_value(schema, value, validated_schema_items)
        else:
            return _check(schema, value)


def _formdent(x):
    return util.strings.indent(pprint.pformat(x, width=1), 2)


def _update_functions(schema):
    def fn(x):
        if isinstance(x, types.FunctionType):
            filename, linenum = x.__code__.co_filename, x.__code__.co_firstlineno
            x = 'lambda:{filename}:{linenum}'.format(**locals())
        return x
    return fn


def _updater(schema, value):
    return lambda x: _prettify(x + _helpful_message(schema, value))


def _helpful_message(schema, value):
    for fn in [x for x in util.seqs.flatten(schema) if isinstance(x, (types.FunctionType, types.LambdaType))]:
        try:
            filename, linenum = fn.__code__.co_filename, fn.__code__.co_firstlineno
            with open(filename) as f:
                lines = f.read().splitlines()
            start = end = None
            for i in reversed(range(linenum)):
                if not lines[i].strip() or 'def ' in lines[i] or 'class ' in lines[i]:
                    break
                elif ' = ' in lines[i]:
                    start = i
                    break
            if start is None:
                filename, linenum = fn.__code__.co_filename, fn.__code__.co_firstlineno
                schema = 'function:{filename}:{linenum}'.format(**locals())
            else:
                if any(x in lines[start] for x in ['{', '(', '[']):
                    for i in range(linenum, len(lines) + 1):
                        text = '\n'.join(lines[start:i])
                        if all(text.count(x) == text.count(y) for x, y in [('{', '}'), ('[', ']'), ('(', ')')]):
                            end = i
                            break
                if end is not None:
                    schema = '\n'.join(lines[start:end])
                    size = len(lines[start]) - len(lines[start].lstrip())
                    schema = util.strings.unindent(schema, size)
            break
        except:
            continue
    else:
        schema = pprint.pformat(schema, width=1)
    return '\n\nobj:\n{}\nschema:\n{}'.format(
        util.strings.indent(pprint.pformat(value, width=1), 2),
        util.strings.indent(schema, 2),
    )


def _check_for_items_in_value_that_dont_satisfy_schema(schema, value, exact_match):
    validated_schema_items = []
    val = {}
    for k, v in value.items():
        value_match = k in schema
        type_match = type(k) in [x for x in schema if isinstance(x, type)]
        object_match = object in schema
        if value_match or type_match or object_match:
            key = k if value_match else type(k) if type_match else object
            validator = schema[key]
            validated_schema_items.append((key, validator))
            with util.exceptions.update("key:\n  {}".format(k), AssertionError):
                val[k] = _check(validator, v)
        elif exact_match:
            raise AssertionError('{} <{}> does not match schema keys: {}'.format(k, type(k), ', '.join(['{} <{}>'.format(x, type(x)) for x in schema.keys()])))

    return val, validated_schema_items


def _check_for_items_in_schema_missing_in_value(schema, value, validated_schema_items):
    if value or not {type(x) for x in schema.keys()} == {type}: # if schema keys are all types, and value is empty, return
        for k, v in schema.items():
            if k not in value and (k, v) not in validated_schema_items: # only check schema items if they haven't already been satisfied
                if isinstance(k, type): # if a type key is missing, look for an item that satisfies it
                    for vk, vv in value.items():
                        with util.exceptions.ignore(AssertionError):
                            _validate(k, vk)
                            _validate(v, vv)
                            break
                    else:
                        raise AssertionError('{} <{}> is missing (key, value) pair: {} <{}>, {} <{}>'.format(value, type(value), k, type(k), v, type(v)))
                elif isinstance(v, (list, tuple)) and v and v[0] == ':O':
                    assert len(v) == 3, ':O schema should be (:O, schema, default-value), not: {}'.format(v)
                    _, schema, default_value = v
                    value = util.dicts.merge(value, {k: _validate(schema, default_value)})
                else: # TODO is it useful to optionally ignore missing keys in the value?
                    raise AssertionError('{} <{}> is missing required key: {} <{}>'.format(value, type(value), k, type(k)))
    return value


def _starts_with_keyword(x):
    if x and isinstance(x[0], str) and x[0].startswith(':'):
        return True
    else:
        return False


def _check(validator, value):
    with util.exceptions.update(_updater(validator, value), AssertionError):
        # TODO break this up into well named pieces
        assert not isinstance(validator, set), 'a set cannot be a validator: {}'.format(validator)
        if validator is object:
            return value
        elif isinstance(validator, (list, tuple)):
            assert isinstance(value, (list, tuple)) or _starts_with_keyword(validator), '{} <{}> is not a seq: {} <{}>'.format(value, type(value), validator, type(validator))
            if validator and validator[0] in _schema_commands:
                if validator[0] == ':O':
                    assert len(validator) == 3, ':O schema should be (:O, schema, default-value), not: {}'.format(validator)
                    return _check(validator[1], value)
                elif validator[0] == ':U':
                    tracebacks = []
                    for v in validator[1:]:
                        try:
                            value = _check(v, value)
                        except AssertionError as e:
                            tracebacks.append(traceback.format_exc())
                    if len(tracebacks) == len(validator[1:]):
                        raise AssertionError('{} <{}> did not match any of [{}]\n{}'.format(value, type(value), ', '.join(['{} <{}>'.format(x, type(x)) for x in validator[1:]]), '\n'.join(tracebacks)))
                    else:
                        return value
                elif validator[0] == ':I':
                    tracebacks = []
                    for v in validator[1:]:
                        try:
                            value = _check(v, value)
                        except AssertionError as e:
                            tracebacks.append(traceback.format_exc())
                    if tracebacks:
                        raise AssertionError('{} <{}> did not match any of [{}]\n{}'.format(value, type(value), ', '.join(['{} <{}>'.format(x, type(x)) for x in validator[1:]]), '\n'.join(tracebacks)))
                    else:
                        return value
                elif validator[0] == ':fn':
                    assert isinstance(value, types.FunctionType), '{} <{}> is not a function'.format(value, type(value))
                    assert len(validator) in [2, 3], ':fn schema should be (:fn, [<args>...], {<kwargs>: <val>, ...}) or (:fn, [<args>...]), not: {}'.format(validator)
                    args, kwargs = validator[1:]
                    _args, _kwargs = value._schema
                    assert tuple(_args) == tuple(args), 'pos args {_args} did not match {args}'.format(**locals())
                    assert _kwargs == kwargs, 'kwargs {_kwargs} did not match {kwargs}'.format(**locals())
                    return value
            elif isinstance(validator, list):
                if not validator:
                    assert not value, 'you schema is an empty sequence, but this is not empty: {}'.format(value)
                elif value:
                    assert len(validator) == 1, 'list validators represent variable length iterables and must contain a single validator: {}'.format(validator)
                return [_check(validator[0], v) for v in value]
            elif isinstance(validator, tuple):
                assert len(validator) == len(value), '{} <{}> mismatched length of validator {} <{}>'.format(value, type(value), validator, type(validator))
                return [_check(_validator, _val) for _validator, _val in zip(validator, value)]
        elif isinstance(validator, dict):
            assert isinstance(value, dict), '{} <{}> does not match schema {} <{}>'.format(value, type(value), validator, type(validator))
            return _validate(validator, value)
        elif isinstance(validator, type):
            assert isinstance(value, validator), '{} <{}> is not a <{}>'.format(value, type(value), validator)
            return value
        elif isinstance(validator, (types.FunctionType, type(callable))):
            assert validator(value), '{} <{}> failed validator {}'.format(value, type(value), util.func.source(validator))
            return value
        elif isinstance(validator, json[1:]):
            with util.exceptions.ignore(AttributeError):
                value = value.decode('utf-8')
            assert value == validator, '{} <{}> != {} <{}>'.format(value, type(value), validator, type(validator))
            return value
        else:
            raise AssertionError('bad validator {} <{}>'.format(validator, type(validator)))


def _prettify(x):
    return re.sub("\<\w+ \'([\w\.]+)\'\>", r'\1', str(x))


def _get_schemas(fn, args, kwargs):
    arg_schemas, kwarg_schemas, return_schema = _read_annotations(fn, args, kwargs)
    schemas = {'yields': kwarg_schemas.pop('yields', object),
               'sends': kwarg_schemas.pop('sends', object),
               'returns': kwarg_schemas.pop('returns', return_schema),
               'args': kwarg_schemas.pop('args', None),
               'kwargs': kwarg_schemas.pop('kwargs', None),
               'arg': arg_schemas,
               'kwarg': kwarg_schemas}
    return schemas


def _read_annotations(fn, arg_schemas, kwarg_schemas):
    if not arg_schemas:
        sig = inspect.signature(fn)
        arg_schemas = [x.annotation
                       for x in sig.parameters.values()
                       if x.default is inspect._empty
                       and x.annotation is not inspect._empty
                       and x.kind is x.POSITIONAL_OR_KEYWORD]
    val = {x.name: x.annotation
           for x in sig.parameters.values()
           if x.default is not inspect._empty
           or x.kind is x.KEYWORD_ONLY
           and x.annotation is not inspect._empty}
    val = util.dicts.merge(val,
                        {'args': x.annotation
                         for x in sig.parameters.values()
                         if x.annotation is not inspect._empty
                         and x.kind is x.VAR_POSITIONAL})
    val = util.dicts.merge(val,
                        {'kwargs': x.annotation
                         for x in sig.parameters.values()
                         if x.annotation is not inspect._empty
                         and x.kind is x.VAR_KEYWORD})
    kwarg_schemas = util.dicts.merge(kwarg_schemas, val)
    if sig.return_annotation is not inspect._empty:
        return_schema = sig.return_annotation
    else:
        return_schema = object
    return arg_schemas, kwarg_schemas, return_schema


def _check_args(args, kwargs, name, schemas):
    with util.exceptions.update(_prettify, AssertionError):
        # TODO better to use inspect.getcallargs() for this? would change the semantics of pos arg checking. hmmn...
        # look at the todo in util.web.post for an example.
        assert len(schemas['arg']) == len(args) or schemas['args'], 'you asked to check {} for {} pos args, but {} were provided\nargs:\n{}\nschema:\n{}'.format(
            name, len(schemas['arg']), len(args), pprint.pformat(args, width=1), pprint.pformat(schemas, width=1)
        )
        _args = []
        for i, (schema, arg) in enumerate(zip(schemas['arg'], args)):
            with util.exceptions.update('pos arg num:\n  {}'.format(i), AssertionError):
                _args.append(validate(schema, arg))
        if schemas['args'] and args[len(schemas['arg']):]:
            _args += validate(schemas['args'], args[len(schemas['arg']):])
        _kwargs = {}
        for k, v in kwargs.items():
            if k in schemas['kwarg']:
                with util.exceptions.update('keyword arg:\n  {}'.format(k), AssertionError):
                    _kwargs[k] = validate(schemas['kwarg'][k], v)
            elif schemas['kwargs']:
                with util.exceptions.update('keyword args schema failed.', AssertionError):
                    _kwargs[k] = validate(schemas['kwargs'], {k: v})[k]
            else:
                raise AssertionError('cannot check {} for unknown key: {}={}'.format(name, k, v))
        return _args, _kwargs


def _fn_check(decoratee, name, schemas):
    @functools.wraps(decoratee)
    def decorated(*args, **kwargs):
        args = util.data.freeze(args)
        kwargs = util.data.freeze(kwargs)
        with util.exceptions.update('schema.check failed for function:\n  {}'.format(name), AssertionError, when=lambda x: 'failed for ' not in x):
            if args and inspect.ismethod(getattr(args[0], decoratee.__name__, None)):
                a, kwargs = _check_args(args[1:], kwargs, name, schemas)
                args = [args[0]] + a
            else:
                args, kwargs = _check_args(args, kwargs, name, schemas)
        value = decoratee(*args, **kwargs)
        with util.exceptions.update('schema.check failed for return value of function:\n {}'.format(name), AssertionError):
            output = validate(schemas['returns'], value)
        return output
    return decorated


def _gen_check(decoratee, name, schemas):
    @functools.wraps(decoratee)
    def decorated(*args, **kwargs):
        args = util.data.freeze(args)
        kwargs = util.data.freeze(kwargs)
        with util.exceptions.update('schema.check failed for generator:\n  {}'.format(name), AssertionError, when=lambda x: 'failed for ' not in x):
            if args and inspect.ismethod(getattr(args[0], decoratee.__name__, None)):
                a, kwargs = _check_args(args[1:], kwargs, name, schemas)
                args = [args[0]] + a
            else:
                args, kwargs = _check_args(args, kwargs, name, schemas)
        generator = decoratee(*args, **kwargs)
        to_send = None
        first_send = True
        send_exception = False
        while True:
            if not first_send:
                with util.exceptions.update('schema.check failed for send value of generator:\n {}'.format(name), AssertionError):
                    to_send = validate(schemas['sends'], to_send)
            first_send = False
            try:
                if send_exception:
                    to_yield = generator.throw(*send_exception)
                    send_exception = False
                else:
                    to_yield = generator.send(to_send)
                with util.exceptions.update('schema.check failed for yield value of generator:\n {}'.format(name), AssertionError):
                    to_yield = validate(schemas['yields'], to_yield)
            except StopIteration as e:
                with util.exceptions.update('schema.check failed for return value of generator:\n {}'.format(name), AssertionError):
                    e.value = validate(schemas['returns'], getattr(e, 'value', None))
                raise
            try:
                to_send = yield to_yield
            except:
                send_exception = sys.exc_info()
    return decorated


@util.func.optionally_parameterized_decorator
def check(*args, **kwargs):
    # TODO add doctest with :fn and args/kwargs
    def decorator(decoratee):
        if disabled:
            return decoratee
        name = util.func.name(decoratee)
        schemas = _get_schemas(decoratee, args, kwargs)
        if inspect.isgeneratorfunction(decoratee):
            decorated = _gen_check(decoratee, name, schemas)
        else:
            decorated = _fn_check(decoratee, name, schemas)
        decorated._schema = schemas['arg'], {k: v for k, v in list(schemas['kwarg'].items()) + [['returns', schemas['returns']]]}
        return decorated
    return decorator
