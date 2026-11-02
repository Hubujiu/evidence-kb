const $=id=>document.getElementById(id);const failureOwners=new WeakMap();let currentBlob=null,viewRevision=0,citationSelection=null;
function clearView(){viewRevision++;citationSelection=null;$('results').replaceChildren();$('citation').replaceChildren();if(currentBlob){URL.revokeObjectURL(currentBlob);currentBlob=null;} }
function message(text){$('message').textContent=text;}
function currentScope(){return JSON.stringify([viewRevision,$('kb').value,$('token').value]);}
function current(stamp){
  if(stamp!==currentScope()){
    const error=new Error('范围已切换，已丢弃之前的响应。');
    error.stale=true;
    throw error;
  }
}
function currentCitation(owner){
  current(owner.scope);
  if(owner.selection!==citationSelection){
    const error=new Error('已选择其他证据，旧响应已丢弃。');
    error.stale=true;
    throw error;
  }
}
function ownedFailure(error,owner){
  const failure=new Error(error.message);
  failure.stale=error.stale===true;
  failureOwners.set(failure,owner);
  return failure;
}
function currentFailure(error){
  const owner=failureOwners.get(error);
  return !error.stale&&(!owner||(
    owner.scope===currentScope()&&
    (owner.selection===undefined||owner.selection===citationSelection)));
}
async function request(path,options={}){
  const stamp=currentScope();
  const headers={Authorization:'Bearer '+$('token').value,...options.headers};
  if(options.body&&!(options.body instanceof FormData))headers['Content-Type']='application/json';
  try{
    const response=await fetch(path,{...options,headers});
    current(stamp);
    if(!response.ok){
      let detail;
      try{detail=await response.json();}catch{detail={detail:'请求未完成'};}
      current(stamp);
      throw new Error(response.status+' '+(detail.detail||detail.error||'请求失败'));
    }
    return response;
  }catch(error){throw ownedFailure(error,{scope:stamp});}
}
async function json(path,options={}){
  const stamp=currentScope();
  try{
    const response=await request(path,options);
    const result=await response.json();
    current(stamp);
    return result;
  }catch(error){throw ownedFailure(error,{scope:stamp});}
}
async function blob(path){
  const stamp=currentScope();
  try{
    const response=await request(path);
    const result=await response.blob();
    current(stamp);
    return result;
  }catch(error){throw ownedFailure(error,{scope:stamp});}
}
function scope(){const id=$('kb').value;if(!id)throw new Error('请先选择知识库。');return'/knowledge-bases/'+encodeURIComponent(id);}
async function refresh(selected){const items=await json('/knowledge-bases');$('kb').replaceChildren();for(const item of items){const option=document.createElement('option');option.value=item.kb_id;option.textContent=item.name;$('kb').append(option);}if(selected)$('kb').value=selected;}
async function action(button,fn){
  button.disabled=true;
  message('');
  try{await fn();}
  catch(error){if(currentFailure(error))message(error.message);}
  finally{button.disabled=false;}
}
$('connect').onclick=()=>action($('connect'),async()=>{clearView();const id=await json('/identity');$('identity').textContent=id.tenant_id+' / '+id.principal_id;await refresh();message('身份已验证。');});
$('create').onclick=()=>action($('create'),async()=>{const kb=await json('/knowledge-bases',{method:'POST',body:JSON.stringify({name:$('name').value})});await refresh(kb.kb_id);clearView();message('已创建知识库。');});
$('kb').onchange=()=>{clearView();$('ingest').textContent='已切换知识库，当前视图已清空。';};
$('token').oninput=()=>{clearView();$('identity').textContent='身份已更改，请重新连接。';$('kb').replaceChildren();};
$('upload').onclick=()=>action($('upload'),async()=>{const file=$('file').files[0];if(!file)throw new Error('请选择文件。');const data=new FormData();data.append('file',file);const stamp=currentScope();const result=await json(scope()+'/sources',{method:'POST',body:data});current(stamp);$('ingest').textContent=file.name+' · '+result.document.status+' · '+result.document.elements.length+' 个结构元素';message('文件已解析。构建并发布后可检索。');});
$('publish').onclick=()=>action($('publish'),async()=>{const prefix=scope();const build=await json(prefix+'/builds',{method:'POST'});const manifest=await json(prefix+'/builds/'+build.build_id+'/publish',{method:'POST'});message('索引已发布。版本 '+manifest.manifest_id);});
async function showCitation(id){
  const owner={scope:currentScope(),selection:{}};
  citationSelection=owner.selection;
  try{
    const prefix=scope();
    const data=await json(prefix+'/citations/'+id);
    currentCitation(owner);
    const container=$('citation');
    container.replaceChildren();
    const title=document.createElement('h3');title.textContent=data.filename;container.append(title);
    const version=document.createElement('p');version.className='muted';version.textContent='原件版本 '+data.source_version_id;container.append(version);
    const excerpt=document.createElement('pre');const mark=document.createElement('mark');mark.textContent=data.text;excerpt.append(mark);container.append(excerpt);
    const location=document.createElement('pre');location.textContent=JSON.stringify(data.anchors,null,2);container.append(location);
    if(data.media_type==='application/pdf'){
      const preview=await blob(prefix+'/citations/'+id+'/preview');
      currentCitation(owner);
      if(currentBlob)URL.revokeObjectURL(currentBlob);
      currentBlob=URL.createObjectURL(preview);
      const image=document.createElement('img');image.src=currentBlob;image.alt='PDF 原页面与证据高亮';container.prepend(image);
    }
    const download=document.createElement('button');download.textContent='下载原件';download.className='secondary';
    download.onclick=()=>action(download,async()=>{
      const original=await blob(prefix+'/citations/'+id+'/original');
      const url=URL.createObjectURL(original);
      const link=document.createElement('a');link.href=url;link.download=data.filename;link.click();
      setTimeout(()=>URL.revokeObjectURL(url),1000);
    });
    container.append(download);
  }catch(error){throw ownedFailure(error,owner);}
}
$('search').onclick=()=>action($('search'),async()=>{clearView();const data=await json(scope()+'/search',{method:'POST',body:JSON.stringify({query:$('query').value,channel:$('channel').value})});for(const hit of data.hits){const block=document.createElement('div');block.className='hit';const text=document.createElement('p');text.textContent=hit.text;const button=document.createElement('button');button.className='secondary';button.textContent='查看原文与定位';button.onclick=()=>action(button,()=>showCitation(hit.citation_id));block.append(text,button);$('results').append(block);}if(!data.hits.length){const empty=document.createElement('p');empty.textContent='当前授权范围内未找到匹配证据。';$('results').append(empty);}message('当前库返回 '+data.hits.length+' 条证据。');});
$('query').onkeydown=e=>{if(e.key==='Enter')$('search').click();};
