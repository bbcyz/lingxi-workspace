package cn.pppyl.lingxiworkspace;

import com.caoccao.javet.annotations.V8Function;
import com.caoccao.javet.enums.JSRuntimeType;
import com.caoccao.javet.interop.V8Host;
import com.caoccao.javet.interop.V8Runtime;
import com.caoccao.javet.values.V8Value;
import com.caoccao.javet.values.reference.V8ValueGlobalObject;
import com.caoccao.javet.values.reference.V8ValuePromise;
import com.chaquo.python.PyObject;
import org.json.JSONObject;

/** Browser-free V8 boundary for the Python member-benefits protocol runtime. */
public final class MemberBenefitsV8 {
    private static final String TIMERS =
            "(() => {" +
            "let nextId=1; const timers=new Map();" +
            "globalThis.setTimeout=(callback,delay=0,...args)=>{" +
            " const id=nextId++; timers.set(id,{callback,args,due:Date.now()+Math.max(1,Number(delay)||1)}); return id; };" +
            "globalThis.clearTimeout=id=>timers.delete(id);" +
            "globalThis.__run_due_timers=()=>{" +
            " const now=Date.now();" +
            " for(const [id,timer] of [...timers]) if(timer.due<=now){timers.delete(id);timer.callback(...timer.args);}" +
            " let wait=-1; for(const timer of timers.values()){const delay=Math.max(0,timer.due-Date.now());wait=wait<0?delay:Math.min(wait,delay);}" +
            " return wait; };" +
            "})();";

    private MemberBenefitsV8() {}

    public static final class PythonBridge {
        private final PyObject callback;

        PythonBridge(PyObject callback) {
            this.callback = callback;
        }

        @V8Function(name = "__py_http_json")
        public String request(String payload) {
            if (callback == null) return "{\"error\":\"未配置 Python HTTP 传输\"}";
            try (PyObject result = callback.call(payload)) {
                return result.toString();
            } catch (Throwable error) {
                return "{\"error\":" + JSONObject.quote(String.valueOf(error.getMessage())) + "}";
            }
        }
    }

    private static V8Runtime createRuntime(String configJson, PythonBridge bridge) throws Exception {
        V8Runtime runtime = V8Host.getV8Instance().createV8Runtime();
        try {
            try (V8ValueGlobalObject global = runtime.getGlobalObject()) {
                global.bind(bridge);
                global.set("__member_config_json", configJson);
            }
            runtime.getExecutor(TIMERS).executeVoid();
            runtime.getExecutor("globalThis.__PY_CONFIG=JSON.parse(globalThis.__member_config_json);").executeVoid();
            return runtime;
        } catch (Exception error) {
            runtime.close();
            throw error;
        }
    }

    private static String await(V8Runtime runtime, V8ValuePromise promise, int timeoutMs) throws Exception {
        long deadline = android.os.SystemClock.uptimeMillis() + timeoutMs;
        while (promise.isPending()) {
            runtime.await();
            Long wait = runtime.getExecutor("globalThis.__run_due_timers()").executeLong();
            runtime.await();
            if (!promise.isPending()) break;
            if (android.os.SystemClock.uptimeMillis() >= deadline) throw new Exception("V8 计算超时");
            android.os.SystemClock.sleep(wait == null || wait < 0 ? 1 : Math.max(1, Math.min(wait, 10)));
        }
        if (promise.isRejected()) {
            try (V8Value reason = promise.getResult()) {
                throw new Exception("V8 计算失败: " + reason);
            }
        }
        return promise.getResultString();
    }

    public static String runWheel(
            String configJson, String runtimeSource, String glueSource, String wasmBase64,
            PyObject callback, int timeoutMs) throws Exception {
        PythonBridge bridge = new PythonBridge(callback);
        try (V8Runtime runtime = createRuntime(configJson, bridge)) {
            runtime.getExecutor(runtimeSource + "\n//# sourceURL=python-wheel-runtime.js").executeVoid();
            runtime.getExecutor(glueSource).executeVoid();
            try (V8ValueGlobalObject global = runtime.getGlobalObject()) {
                global.set("__member_wasm_base64", wasmBase64);
            }
            runtime.getExecutor(
                    "globalThis.__wheel_wasm_bytes=Uint8Array.from(atob(globalThis.__member_wasm_base64),c=>c.charCodeAt(0));")
                    .executeVoid();
            try (V8ValuePromise promise = runtime.getExecutor("__wheel_run()").execute()) {
                return await(runtime, promise, timeoutMs);
            }
        }
    }

    public static String runDfp(
            String configJson, String runtimeSource, String dfpRuntimeSource, String sdkSource,
            String dfpConfigJson, String stateJson, PyObject callback, int timeoutMs) throws Exception {
        PythonBridge bridge = new PythonBridge(callback);
        try (V8Runtime runtime = createRuntime(configJson, bridge)) {
            runtime.getExecutor(runtimeSource + "\n//# sourceURL=python-dfp-environment.js").executeVoid();
            runtime.getExecutor(dfpRuntimeSource + "\n//# sourceURL=python-dfp-runtime.js").executeVoid();
            try (V8ValueGlobalObject global = runtime.getGlobalObject()) {
                global.set("__DFP_SDK_SOURCE", sdkSource);
                global.set("__member_dfp_config_json", dfpConfigJson);
                global.set("__member_dfp_state_json", stateJson);
            }
            try (V8ValuePromise promise = runtime.getExecutor(
                    "__dfp_runtime_run(__DFP_SDK_SOURCE,JSON.parse(__member_dfp_config_json),JSON.parse(__member_dfp_state_json))")
                    .execute()) {
                return await(runtime, promise, timeoutMs);
            }
        }
    }
}
