'use strict';
const $ = id => document.getElementById(id);
let current = null, selection = 0;
const labels = {date:'Date',amount:'Total amount',tax:'Tax',currency:'Currency',vendor:'Vendor',buyer:'Buyer',invoice_no:'Invoice number',invoice_code:'Invoice code',check_code:'Check code',payment_method:'Payment method',category:'Category',note:'Note'};
async function api(path, body) {
  const response = await fetch(path, body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {});
  const result = await response.json();
  if (!result.ok) throw new Error(result.error.message);
  return result.data;
}
function message(text) {$('message').textContent=text;}
async function load() {
  const rows=await api('/api/records?status='+encodeURIComponent($('status').value));
  $('count').textContent=rows.length; $('list-count').textContent=rows.length;
  $('records').replaceChildren();
  for (const row of rows) {
    const button=document.createElement('button');button.className='receipt'+(current?.id===row.id?' selected':'');
    const strong=document.createElement('strong');strong.textContent=row.vendor||'Unnamed receipt';
    const line=document.createElement('div'), date=document.createElement('span'),amount=document.createElement('span');
    date.textContent=row.date||'Date missing';amount.textContent=(row.currency+' '+row.amount).trim();amount.className='amount';line.append(date,amount);button.append(strong,line);
    if(row.duplicate_of){const duplicate=document.createElement('div');duplicate.textContent='Possible duplicate';button.append(duplicate);}
    button.onclick=()=>choose(row.id).catch(e=>message(e.message));$('records').append(button);
  }
}
async function choose(id) {
  const token=++selection, item=await api('/api/document?id='+encodeURIComponent(id));
  if(token!==selection)return;current=item;
  $('empty').hidden=true;$('detail').hidden=false;$('vendor').textContent=item.record.vendor||'Unnamed receipt';$('state').textContent=item.status==='reviewed'?'Reviewed':'Needs review';
  $('warnings').replaceChildren();
  for(const warning of [...item.details.warnings,...(item.duplicate_of?['Possible duplicate of '+item.duplicate_of]:[])]){const p=document.createElement('p');p.textContent=warning;$('warnings').append(p);}
  $('page').replaceChildren();for(const page of item.details.pages){const option=document.createElement('option');option.value=page.page;option.textContent='Page '+page.page;$('page').append(option);}
  $('fields').replaceChildren();
  for(const [name,label] of Object.entries(labels)) {
    const field=item.fields[name], wrapper=document.createElement('div');wrapper.className='field'+(field.low_confidence?' low':'')+(['vendor','buyer','note','check_code'].includes(name)?' wide':'');
    const heading=document.createElement('label');heading.htmlFor='field-'+name;heading.textContent=label;
    const confidence=document.createElement('span');confidence.className='confidence';confidence.textContent=field.edited?'edited':Math.round(field.confidence*100)+'%';heading.append(confidence);
    const input=document.createElement('input');input.id='field-'+name;input.name=name;input.value=field.value;
    input.addEventListener('focus',()=>highlight(field.sources));wrapper.append(heading,input);$('fields').append(wrapper);
  }
  preview();await load();message('Select any field to locate its source. Save after reviewing.');
}
function preview(){
  const page=current.details.pages.find(p=>p.page===Number($('page').value));
  $('preview').hidden=!page;$('overlay').replaceChildren();
  if(page){$('preview').src='/api/preview?id='+current.id+'&page='+page.page;$('overlay').setAttribute('viewBox',`0 0 ${page.width} ${page.height}`);}
}
function highlight(sources){
  if(!sources.length){$('overlay').replaceChildren();return;}
  $('page').value=sources[0].page;preview();
  for(const source of sources.filter(s=>s.page===Number($('page').value))){
    const [x,y,x2,y2]=source.bbox,rect=document.createElementNS('http://www.w3.org/2000/svg','rect');
    for(const [key,value]of Object.entries({x,y,width:x2-x,height:y2-y,class:'highlight'}))rect.setAttribute(key,value);$('overlay').append(rect);
  }
}
$('page').onchange=preview;
$('status').onchange=()=>load().catch(e=>message(e.message));
$('scan').onsubmit=async event=>{event.preventDefault();$('scan-button').disabled=true;message('Processing locally. OCR can take a moment…');try{const result=await api('/api/scan',{path:$('path').value});await load();message(`${result.count} file(s) processed. ${result.errors.length} error(s).`);if(result.errors.length)message(result.errors.map(e=>e.message).join('; '));}catch(e){message(e.message);}finally{$('scan-button').disabled=false;}};
$('fields-form').onsubmit=async event=>{event.preventDefault();if(!current)return;const id=current.id, fields={};for(const [name,value]of new FormData(event.target))if(value!==current.fields[name].value)fields[name]=value;try{await api('/api/update',{id,fields});if(current.id===id)await choose(id);message('Reviewed details saved. Exports now use these values.');}catch(e){message(e.message);}};
load().catch(e=>message(e.message));
