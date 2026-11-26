"""Keep the SQLite credential encryption key in the macOS user's keychain."""

from contextlib import ExitStack, contextmanager
import ctypes
from functools import lru_cache
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_SERVICE = b'cn.pppyl.lingxiworkspace.credentials'
_ACCOUNT = b'sqlite-aes256-gcm-v1'
_FORMAT = b'LingxiWorkspace-macos\x01'
_NONCE_SIZE = 12
_TAG_SIZE = 16
_KEY_SIZE = 32
_ITEM_NOT_FOUND = -25300
_DUPLICATE_ITEM = -25299
_UTF8 = 0x08000100


class KeychainError(Exception):
    pass


def _bind(library, name, result_type, *argument_types):
    function = getattr(library, name)
    function.restype = result_type
    function.argtypes = argument_types
    return function


class _Keychain:
    def __init__(self):
        security = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        core = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        pointer = ctypes.c_void_p
        pointers = ctypes.POINTER(pointer)
        self._copy_matching = _bind(security, 'SecItemCopyMatching', ctypes.c_int32, pointer, pointers)
        self._add_item = _bind(security, 'SecItemAdd', ctypes.c_int32, pointer, pointers)
        self._new_string = _bind(core, 'CFStringCreateWithCString', pointer, pointer, ctypes.c_char_p, ctypes.c_uint32)
        self._new_data = _bind(core, 'CFDataCreate', pointer, pointer, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_long)
        self._data_length = _bind(core, 'CFDataGetLength', ctypes.c_long, pointer)
        self._data_bytes = _bind(core, 'CFDataGetBytePtr', pointer, pointer)
        self._new_dictionary = _bind(core, 'CFDictionaryCreate', pointer, pointer, pointers, pointers,
                                     ctypes.c_long, pointer, pointer)
        self._release = _bind(core, 'CFRelease', None, pointer)
        self._key_callbacks = ctypes.c_byte.in_dll(core, 'kCFTypeDictionaryKeyCallBacks')
        self._value_callbacks = ctypes.c_byte.in_dll(core, 'kCFTypeDictionaryValueCallBacks')
        self._true = pointer.in_dll(core, 'kCFBooleanTrue').value
        self._attributes = {
            name: pointer.in_dll(security, name).value
            for name in ('kSecClass', 'kSecClassGenericPassword', 'kSecAttrService', 'kSecAttrAccount',
                         'kSecReturnData', 'kSecValueData')
        }

    @contextmanager
    def _item(self, key: bytes | None = None):
        with ExitStack() as owned:
            def own(value):
                if not value:
                    raise KeychainError('无法创建 macOS 钥匙串查询。')
                owned.callback(self._release, value)
                return value

            attributes = self._attributes
            item = {
                attributes['kSecClass']: attributes['kSecClassGenericPassword'],
                attributes['kSecAttrService']: own(self._new_string(None, _SERVICE, _UTF8)),
                attributes['kSecAttrAccount']: own(self._new_string(None, _ACCOUNT, _UTF8)),
            }
            if key is None:
                item[attributes['kSecReturnData']] = self._true
            else:
                key_buffer = (ctypes.c_ubyte * len(key)).from_buffer_copy(key)
                item[attributes['kSecValueData']] = own(self._new_data(None, key_buffer, len(key)))
            array_type = ctypes.c_void_p * len(item)
            query = self._new_dictionary(
                None, array_type(*item), array_type(*item.values()), len(item),
                ctypes.byref(self._key_callbacks), ctypes.byref(self._value_callbacks),
            )
            yield own(query)

    def read(self) -> bytes | None:
        result = ctypes.c_void_p()
        with self._item() as query:
            status = self._copy_matching(query, ctypes.byref(result))
        if status == _ITEM_NOT_FOUND:
            return None
        if status != 0:
            raise KeychainError(f'无法读取 macOS 钥匙串中的凭据密钥（系统代码 {status}）。')
        if not result:
            raise KeychainError('macOS 钥匙串未返回凭据密钥。')
        try:
            return ctypes.string_at(self._data_bytes(result), self._data_length(result))
        finally:
            self._release(result)

    def add(self, key: bytes) -> bool:
        with self._item(key) as attributes:
            status = self._add_item(attributes, None)
        if status not in (0, _DUPLICATE_ITEM):
            raise KeychainError(f'无法在 macOS 钥匙串中保存凭据密钥（系统代码 {status}）。')
        return status == 0


@lru_cache(maxsize=1)
def _keychain():
    try:
        return _Keychain()
    except OSError as error:
        raise KeychainError('无法加载 macOS 钥匙串服务。') from error


def crypt_credentials(data: bytes, *, decrypt=False) -> bytes:
    nonce_start = len(_FORMAT)
    nonce_end = nonce_start + _NONCE_SIZE
    if decrypt and (not data.startswith(_FORMAT) or len(data) < nonce_end + _TAG_SIZE):
        raise KeychainError('无法读取此记录的 macOS 加密格式，已保留原数据。')

    keychain = _keychain()
    key = keychain.read()
    if key is None and not decrypt:
        candidate = AESGCM.generate_key(bit_length=_KEY_SIZE * 8)
        # SecItemAdd never replaces another process's key on a duplicate item.
        key = candidate if keychain.add(candidate) else keychain.read()
    if key is None:
        raise KeychainError('macOS 钥匙串中找不到凭据加密密钥，已保留原数据。')
    if len(key) != _KEY_SIZE:
        raise KeychainError('macOS 钥匙串中的凭据密钥格式无效。')

    cipher = AESGCM(key)
    if decrypt:
        try:
            return cipher.decrypt(data[nonce_start:nonce_end], data[nonce_end:], _FORMAT)
        except InvalidTag as error:
            raise KeychainError('无法解密此记录，请确认使用原 macOS 用户的钥匙串。') from error
    nonce = secrets.token_bytes(_NONCE_SIZE)
    return _FORMAT + nonce + cipher.encrypt(nonce, data, _FORMAT)
