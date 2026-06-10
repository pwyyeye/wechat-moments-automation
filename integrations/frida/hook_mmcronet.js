var mc = Process.findModuleByName("mmcronet.dll");
if (!mc) throw new Error("mmcronet.dll not loaded");

var ws2 = Process.findModuleByName("ws2_32.dll");
var sendActive = false;

// Hook socket send 来标记 "朋友圈正在发送中"
['send','WSASend'].forEach(function(fn){
    var exp = null;
    ws2.enumerateExports().filter(function(e){return e.type==='function'&&e.name===fn}).forEach(function(e){exp=e;});
    if(!exp)return;
    Interceptor.attach(exp.address,{onEnter:function(args){
        var buf=fn==='send'?args[1]:args[1].readPointer().add(8).readPointer();
        var len=fn==='send'?args[2].toInt32():args[1].readU32();
        if(len>5000&&len<500000){try{var h=buf.readByteArray(50);var s="";var a=new Uint8Array(h);for(var i=0;i<50;i++)s+=(a[i]>=32&&a[i]<127)?String.fromCharCode(a[i]):'.';if(s.indexOf('mmtls')>=0){sendActive=true;setTimeout(function(){sendActive=false},2000);}}}catch(e){}}
    }});
});

// Hook mmcronet — 只在朋友圈发送激活时记录
mc.enumerateExports().filter(function(e){return e.type==='function'}).forEach(function(e){
    try{Interceptor.attach(e.address,{onEnter:function(args){
        if(!sendActive)return;
        for(var i=0;i<4;i++){if(!args[i]||args[i].isNull())continue;
            try{var pk=args[i].readByteArray(512);var a=new Uint8Array(pk);var r=0;
                for(var j=0;j<200;j++){var b=a[j];if((b>=0x20&&b<0x7f)||b===0x0a||b===0x12||b===0x1a)r++;}
                if(r>15){console.log("\n["+e.name+"] arg"+i+" rdbl="+r);console.log(hexdump(args[i],{length:512}));}
            }catch(ex){}
        }
    }});}catch(ex){}
});
console.log("[+] Post Moments now — only mmtls windows will log.");
