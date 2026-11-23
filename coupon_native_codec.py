"""Pure-Python codec for the official recharge native M1 device protocol."""
import hashlib,io,struct,zipfile

SDK_VERSION='v7_6_4r_61803305'

def xxtea_encrypt(data,key):
    if not data:return b''
    words=list(struct.unpack('<'+'I'*((len(data)+3)//4),data.ljust((len(data)+3)//4*4,b'\0')))+[len(data)]
    keys=struct.unpack('<4I',key[:16].split(b'\0')[0].ljust(16,b'\0'))
    n=len(words)-1;z=words[n];total=0
    for _ in range(6+52//len(words)):
        total=(total+0x9e3779b9)&0xffffffff;e=(total>>2)&3
        for p in range(n):
            y=words[p+1];mix=(((z>>5)^(y<<2))+((y>>3)^(z<<4)))^((total^y)+(keys[(p&3)^e]^z))
            words[p]=(words[p]+mix)&0xffffffff;z=words[p]
        y=words[0];mix=(((z>>5)^(y<<2))+((y>>3)^(z<<4)))^((total^y)+(keys[(n&3)^e]^z))
        words[n]=(words[n]+mix)&0xffffffff;z=words[n]
    return struct.pack('<'+'I'*len(words),*words)

def xxtea_decrypt(data,key):
    if not data:return b''
    if len(data)%4:raise ValueError('Invalid XXTEA ciphertext length')
    words=list(struct.unpack('<'+'I'*(len(data)//4),data));n=len(words)-1
    keys=struct.unpack('<4I',key[:16].split(b'\0')[0].ljust(16,b'\0'))
    total=((6+52//len(words))*0x9e3779b9)&0xffffffff;y=words[0]
    while total:
        e=(total>>2)&3
        for p in range(n,0,-1):
            z=words[p-1];mix=(((z>>5)^(y<<2))+((y>>3)^(z<<4)))^((total^y)+(keys[(p&3)^e]^z))
            words[p]=(words[p]-mix)&0xffffffff;y=words[p]
        z=words[n];mix=(((z>>5)^(y<<2))+((y>>3)^(z<<4)))^((total^y)+(keys[e]^z))
        words[0]=(words[0]-mix)&0xffffffff;y=words[0];total=(total-0x9e3779b9)&0xffffffff
    length=words[-1];maximum=n*4
    if not maximum-3<=length<=maximum:raise ValueError('Invalid XXTEA plaintext length')
    return struct.pack('<'+'I'*n,*words[:-1])[:length]

def varint(value):
    if value<0:value&=0xffffffffffffffff
    result=bytearray()
    while value>127:result.append((value&127)|128);value>>=7
    return bytes(result)+bytes([value])

def field(number,value):
    if isinstance(value,int):return varint(number<<3)+varint(value)
    if isinstance(value,str):value=value.encode('utf-8')
    return varint(number<<3|2)+varint(len(value))+value

def message(values):return b''.join(field(number,value)for number,value in values)

def parse_message(data):
    at=0;result={}
    def take_varint():
        nonlocal at
        value=0;shift=0
        while True:
            byte=data[at];at+=1;value|=(byte&127)<<shift
            if byte<128:return value
            shift+=7
    while at<len(data):
        tag=take_varint();number,wire=tag>>3,tag&7
        if wire==0:value=take_varint()
        elif wire==2:
            length=take_varint();value=data[at:at+length];at+=length
        elif wire in (1,5):
            length=8 if wire==1 else 4;value=data[at:at+length];at+=length
        else:raise ValueError('Unsupported protobuf wire type')
        result.setdefault(number,[]).append(value)
    return result

def request_packet(app_id,pairs,*,package_name=None,software_version=None,software_version_code=None,android_api=None,previous_token=None):
    # Numeric fields/ordering follow the retained native protobuf descriptors
    # and assignments in functions 0x9d950 / 0x82968, not guessed JSON names.
    header=[(1,SDK_VERSION)]
    if software_version is not None:header.append((2,software_version))
    if software_version_code is not None:header.append((3,software_version_code))
    header.extend([(4,app_id),(5,0)])
    if android_api is not None:header.append((6,str(android_api)))
    if previous_token is not None:header.append((7,previous_token))
    header.append((8,1))
    if package_name is not None:header.append((9,package_name))
    header.append((10,3))
    body=message((1,message([(1,key),(2,value)]))for key,value in sorted(pairs.items())if value!='')
    plain=message([(1,message(header)),(2,1002),(3,body)])
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=5)as archive:
        archive.writestr(zipfile.ZipInfo('data',(1980,1,1,0,0,0)),plain,compress_type=zipfile.ZIP_DEFLATED,compresslevel=5)
    key=hashlib.sha1(app_id.encode()*3).hexdigest().lower().encode()[:16]
    return plain,xxtea_encrypt(stream.getvalue(),key),hashlib.md5(app_id.encode()+plain+app_id.encode()).hexdigest()

def response_packet(app_id,data):
    key=hashlib.sha1(app_id.encode()*3).hexdigest().lower().encode()[:16]
    decrypted=xxtea_decrypt(data,key)
    with zipfile.ZipFile(io.BytesIO(decrypted))as archive:plain=archive.read('data')
    outer=parse_message(plain)
    return outer,parse_message(outer[2][0]) if outer.get(2)else None
