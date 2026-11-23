package cn.pppyl.lingxiworkspace;

import android.content.Context;
import android.util.Base64;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import org.junit.Test;
import org.junit.runner.RunWith;
import java.io.InputStream;
import java.io.ByteArrayOutputStream;
import static org.junit.Assert.assertTrue;

@RunWith(AndroidJUnit4.class)
public final class RuntimeSmokeTest {
    private String fixture(Context context, String name) throws Exception {
        try (InputStream stream=context.getAssets().open(name);
             ByteArrayOutputStream output=new ByteArrayOutputStream()) {
            byte[] block=new byte[8192];
            int count;
            while ((count=stream.read(block))!=-1) output.write(block,0,count);
            return Base64.encodeToString(output.toByteArray(),Base64.NO_WRAP);
        }
    }

    @Test public void packagedPythonClockAndImageRuntime() throws Exception {
        Context target=InstrumentationRegistry.getInstrumentation().getTargetContext();
        Context test=InstrumentationRegistry.getInstrumentation().getContext();
        if (!Python.isStarted()) Python.start(new AndroidPlatform(target));
        Python python=Python.getInstance();
        PyObject builtins=python.getModule("builtins");
        PyObject values=builtins.callAttr("dict");
        values.put("data_dir",target.getFilesDir().getAbsolutePath()+"/isolated-smoke");
        values.put("ground",fixture(test,"image-ground.png"));
        values.put("patch",fixture(test,"image-patch.png"));
        builtins.callAttr("exec","""
            import os
            os.environ['LINGXI_ANDROID']='1'
            os.environ['LINGXI_DATA_DIR']=data_dir
            from app import app
            from runtime_paths import VERSION
            assert VERSION=='1.10.0'
            assert not app.extensions['unified_logins'].public()
            assert 'net_age_surprise_scheduler' in app.extensions
            from activities.net_age_protocol import PythonWheel
            from activities.net_age_surprise_protocol import SurpriseWheel
            from activities.coc_slider import slide_position
            assert slide_position(ground,patch)==137
            from checkin_flash import calibrate_flash_clock
            calibration=calibrate_flash_clock(samples=3)
            assert calibration['state']=='ready' and calibration['samples']==3
            passed=True
            """,values);
        assertTrue(values.get("passed").toBoolean());
    }
}
