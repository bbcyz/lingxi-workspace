# Third-party components

The portable package includes Python, Flask and dependencies, curl_cffi, cryptography, Waitress, pystray, Pillow, and MiniRacer. No browser or Node.js runtime is bundled. The UI opens in the user's default browser. Fixed JavaScript and WebAssembly resources execute in the embedded V8 runtime under Python control.

Component license texts are preserved in `_internal/third-party-notices`. Python license text is included with the runtime notices.

- Python: https://www.python.org/psf/license/
- PyInstaller (bootloader exception): https://pyinstaller.org/en/stable/license.html
- MiniRacer: https://github.com/bpcreech/PyMiniRacer
- Lucide navigation icons (ISC, with Feather portions under MIT): https://github.com/lucide-icons/lucide. The bundled license is in `static/licenses/lucide.txt`.

The release archive checksum is supplied separately as `SHA256SUMS.txt`.

The Android build uses Chaquopy (MIT), Javet (Apache-2.0), OkHttp and Okio (Apache-2.0), Kotlin standard library (Apache-2.0), HTTPX and its dependencies, and Android-compatible Python wheels. Android's installed System WebView displays the local UI only. Business protocol and device calculations run through Python and the bundled V8 library. Android component notices are included in the APK assets under `third-party-notices` and Python distribution metadata.

- Chaquopy: https://github.com/chaquo/chaquopy/blob/master/LICENSE.txt
- Javet: https://github.com/caoccao/Javet/blob/main/LICENSE
- OkHttp: https://github.com/square/okhttp/blob/master/LICENSE.txt
- HTTPX: https://github.com/encode/httpx/blob/master/LICENSE.md
