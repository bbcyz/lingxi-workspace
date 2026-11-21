// Device API inputs for the pinned SDK. Values come from measured APIs, not
// from a captured token or copied canvas fingerprint.
import {getHeapStatistics} from 'node:v8';
import {GlobalFonts} from '@napi-rs/canvas';

// Chromium MemoryInfo bucketization. Algorithm and BSD notice:
// https://chromium.googlesource.com/chromium/src/+/main/third_party/blink/renderer/core/timing/memory_info.cc
const buckets=[];
let next=Math.fround(10_000_000),power=100_000_000,granularity=100_000;
const scale=Math.fround(Math.exp(Math.log(400)/100));
for(let i=0;i<100;i++){
  const size=Math.trunc(next);
  buckets.push(size-size%granularity);
  next=Math.fround(next*scale);
  if(next>=power){power*=10;granularity*=10;}
}
let cachedMemory;
export function chromiumMemory(){
  if(!cachedMemory||Date.now()-cachedMemory.at>=1_200_000){
    const heap=getHeapStatistics();
    const quantize=size=>buckets.find(value=>value>=size)??buckets.at(-1);
    cachedMemory={at:Date.now(),value:{
      jsHeapSizeLimit:quantize(heap.heap_size_limit),
      totalJSHeapSize:quantize(heap.total_physical_size+heap.external_memory),
      usedJSHeapSize:quantize(heap.used_heap_size+heap.external_memory),
    }};
  }
  return {...cachedMemory.value};
}

export function seedDeviceInputs(w,input){
  const profile=w.JSON.parse(JSON.stringify(input));
  for(const [key,value] of Object.entries(profile.navigator))Object.defineProperty(w.navigator,key,{value,configurable:true});
  for(const [key,value] of Object.entries(profile.window))Object.defineProperty(w,key,{value,configurable:true});
  for(const [key,value] of Object.entries(profile.screen))Object.defineProperty(w.screen,key,{value,configurable:true});
  if(profile.fontFile&&!GlobalFonts.registerFromPath(profile.fontFile,'Arial'))throw new Error('Measured Android font could not be loaded');
  const gpu=profile.webgl;
  w.WebGLRenderingContext=class WebGLRenderingContext{
    getParameter(parameter){
      if(!Object.hasOwn(gpu.parameters,String(parameter)))throw new Error('GPU input missing: '+parameter);
      return gpu.parameters[String(parameter)];
    }
    getSupportedExtensions(){return w.Array.from(gpu.supportedExtensions);}
    getExtension(name){
      if(!gpu.supportedExtensions.includes(name))return null;
      return name==='WEBGL_debug_renderer_info'?{UNMASKED_VENDOR_WEBGL:37445,UNMASKED_RENDERER_WEBGL:37446}:{};
    }
    getShaderPrecisionFormat(shader,precision){
      const key=shader+':'+precision;
      if(!Object.hasOwn(gpu.precision,key))throw new Error('GPU precision input missing: '+key);
      return {...gpu.precision[key]};
    }
  };
  const getContext=w.HTMLCanvasElement.prototype.getContext;
  w.HTMLCanvasElement.prototype.getContext=function(type,...args){
    if(type==='webgl'||type==='experimental-webgl')return new w.WebGLRenderingContext();
    return getContext.call(this,type,...args);
  };
}
