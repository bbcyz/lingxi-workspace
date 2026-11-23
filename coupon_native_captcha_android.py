"""Android OpenCV primitives driven by Python, sharing desktop slot ranking."""
import math
from java import jarray,jbyte,jclass,jfloat

from coupon_native_captcha import RechargeCaptchaError,rank_slots,strip_order


def locate_images(raw_images,order_key,official_y):
    if not jclass('org.opencv.android.OpenCVLoader').initLocal():
        raise RechargeCaptchaError('本机图像组件未能启动，请重新打开客户端。','CAPTCHA_IMAGE_FORMAT')
    Mat,MatOfByte=jclass('org.opencv.core.Mat'),jclass('org.opencv.core.MatOfByte')
    Core,Size=jclass('org.opencv.core.Core'),jclass('org.opencv.core.Size')
    CvType,Rect=jclass('org.opencv.core.CvType'),jclass('org.opencv.core.Rect')
    codecs,proc=jclass('org.opencv.imgcodecs.Imgcodecs'),jclass('org.opencv.imgproc.Imgproc')
    matrices=[]
    def mat():
        value=Mat();matrices.append(value);return value
    def byte_rows(value):
        rows,cols=value.rows(),value.cols();buffer=jarray(jbyte)([0]*(rows*cols))
        value.get(0,0,buffer);values=[int(b)&255 for b in buffer]
        return [values[start:start+cols]for start in range(0,len(values),cols)]
    try:
        images=[]
        for i,raw in enumerate(raw_images):
            encoded=MatOfByte(jarray(jbyte)([b if b<128 else b-256 for b in raw]));matrices.append(encoded)
            image=codecs.imdecode(encoded,codecs.IMREAD_COLOR if i==0 else codecs.IMREAD_UNCHANGED)
            matrices.append(image)
            if image.empty():raise ValueError('Image decode failed')
            images.append(image)
        background,piece=images
        if piece.channels()!=4:raise ValueError('Unsupported piece')
        restored=background.clone();matrices.append(restored);order=strip_order(order_key);strip=background.cols()//32
        for target,source in enumerate(order):
            src=background.submat(Rect(source*strip,0,strip,background.rows()))
            dst=restored.submat(Rect(target*strip,0,strip,background.rows()))
            try:src.copyTo(dst)
            finally:src.release();dst.release()
        alpha,mask=mat(),mat();Core.extractChannel(piece,alpha,3)
        proc.threshold(alpha,mask,127,255,proc.THRESH_BINARY)
        contours=jclass('java.util.ArrayList')();hierarchy=mat();contour_input=mask.clone();matrices.append(contour_input)
        proc.findContours(contour_input,contours,hierarchy,proc.RETR_EXTERNAL,proc.CHAIN_APPROX_NONE)
        if not contours.size():return None,background.cols()
        contour=max((contours.get(i)for i in range(contours.size())),key=proc.contourArea)
        points=[[int(p.x),int(p.y)]for p in contour.toArray()]
        for i in range(contours.size()):matrices.append(contours.get(i))
        gray,blur,edges,inverted,distances=mat(),mat(),mat(),mat(),mat()
        proc.cvtColor(restored,gray,proc.COLOR_BGR2GRAY);proc.GaussianBlur(gray,blur,Size(3,3),0)
        proc.Canny(blur,edges,50,150);Core.bitwise_not(edges,inverted)
        proc.distanceTransform(inverted,distances,proc.DIST_L2,5)
        count=distances.rows()*distances.cols();buffer=jarray(jfloat)([0.0]*count);distances.get(0,0,buffer)
        flat=[float(v)for v in buffer];cols=distances.cols()
        distance_rows=[flat[start:start+cols]for start in range(0,len(flat),cols)]
        kernel=Mat.ones(5,5,CvType.CV_8UC1);matrices.append(kernel);inner,dilated=mat(),mat()
        proc.erode(mask,inner,kernel);proc.dilate(mask,dilated,kernel)
        mask_rows,inner_rows,dilated_rows=byte_rows(mask),byte_rows(inner),byte_rows(dilated)
        inside=[[y,x]for y,row in enumerate(inner_rows)for x,value in enumerate(row)if value]
        outside=[[y,x]for y,row in enumerate(dilated_rows)for x,value in enumerate(row)if value and not mask_rows[y][x]]
        raw_y=math.floor(official_y*background.rows()/150+.5)
        return rank_slots(distance_rows,byte_rows(gray),points,inside,outside,piece.rows(),piece.cols(),raw_y),background.cols()
    except (ValueError,jclass('org.opencv.core.CvException'))as error:
        raise RechargeCaptchaError('官方验证图片暂时无法识别。','CAPTCHA_IMAGE_FORMAT')from error
    finally:
        for matrix in matrices:matrix.release()
