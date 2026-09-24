import os, re, sqlite3, hashlib, time
from urllib.parse import urlparse
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title='Vintage Deal Finder EU', page_icon='👖', layout='wide')

PRICEBOOK = [
    {'name':"Vintage Levi's 501",'keywords':['levis 501 vintage','levi\'s 501 vintage','levis 501 made in usa','levis 501 90s'],'resale':65,'great_buy':25,'risk':'Low-Med','signals':['made in usa','made in japan','90s','80s','black','orange tab','selvedge']},
    {'name':"Vintage Levi's 517",'keywords':['levis 517 vintage','levi\'s 517 vintage','levis 517 bootcut','levis 517 made in usa'],'resale':70,'great_buy':25,'risk':'Low-Med','signals':['made in usa','orange tab','black','bootcut','70s','80s','90s']},
    {'name':'Diesel Zathan','keywords':['diesel zathan','diesel zathan y2k','diesel zathan made in italy'],'resale':75,'great_buy':30,'risk':'Medium','signals':['made in italy','y2k','bootcut','whisker','distressed']},
    {'name':'Diesel Zatiny','keywords':['diesel zatiny','diesel zatiny y2k','diesel zatiny made in italy'],'resale':70,'great_buy':30,'risk':'Medium','signals':['made in italy','y2k','bootcut','whisker']},
    {'name':'True Religion Ricky Super T','keywords':['true religion ricky super t','true religion ricky big t','true religion super t'],'resale':75,'great_buy':30,'risk':'High','signals':['super t','big t','flap pocket','horseshoe','made in usa']},
    {'name':'True Religion Joey','keywords':['true religion joey','true religion joey super t','true religion joey vintage'],'resale':68,'great_buy':28,'risk':'High','signals':['super t','flare','bootcut','horseshoe','flap pocket']},
    {'name':'Miss Sixty Y2K','keywords':['miss sixty y2k','miss sixty low rise','miss sixty bootcut','miss sixty flare'],'resale':55,'great_buy':20,'risk':'Medium','signals':['low rise','flare','bootcut','y2k','embroidered','cargo']},
    {'name':'Evisu Made in Japan / No.2','keywords':['evisu made in japan','evisu no 2','evisu no.2','evisu selvedge','evisu vintage'],'resale':135,'great_buy':50,'risk':'High','signals':['made in japan','no 2','no.2','selvedge','seagull','lot']},
    {'name':'Vintage JNCO','keywords':['jnco vintage','jnco jeans','jnco wide leg'],'resale':115,'great_buy':45,'risk':'Medium','signals':['wide leg','huge pocket','vintage','90s','y2k']},
    {'name':'Lee 101 / Riders','keywords':['lee 101 vintage','lee 101 selvedge','lee riders vintage','lee riders made in usa'],'resale':85,'great_buy':30,'risk':'Low-Med','signals':['selvedge','made in usa','made in japan','union made','vintage']},
    {'name':'Carhartt Double Knee','keywords':['carhartt double knee vintage','carhartt double knee usa','carhartt carpenter vintage'],'resale':80,'great_buy':30,'risk':'Medium','signals':['double knee','made in usa','distressed','fade']},
    {'name':'Southpole / Y2K Baggy','keywords':['southpole vintage jeans','southpole baggy y2k','y2k baggy jeans vintage'],'resale':55,'great_buy':20,'risk':'Low-Med','signals':['baggy','embroidered','wide leg','y2k']},
]
REGION_DOMAINS={'Finland':['vinted.fi','tori.fi','huuto.net'],'Nordics':['vinted.fi','tori.fi','tradera.com','sellpy.fi','sellpy.se'],'EU':['vinted.fi','vinted.fr','vinted.de','vinted.nl','vinted.be','vinted.es','vinted.it','vinted.pt','tori.fi','tradera.com','sellpy.fi','sellpy.se','depop.com','grailed.com'],'Europe':['vinted.fi','vinted.fr','vinted.de','vinted.nl','vinted.be','vinted.es','vinted.it','vinted.pt','tori.fi','tradera.com','sellpy.fi','sellpy.se','depop.com','grailed.com']}
DB='/tmp/deals.db'
def db(): return sqlite3.connect(DB)
def init_db():
    with db() as c: c.execute('CREATE TABLE IF NOT EXISTS deals (fp TEXT PRIMARY KEY,title TEXT,source TEXT,url TEXT,buy REAL,resale REAL,profit REAL,roi REAL,score REAL,decision TEXT,created INTEGER)')
init_db()
def norm(s): return re.sub(r'\s+',' ',re.sub(r'[^a-z0-9 ]+',' ',(s or '').lower())).strip()
def match_target(title):
    t=norm(title); best=None; bs=0
    for x in PRICEBOOK:
        for q in x['keywords']:
            sc=sum(1 for tok in norm(q).split() if len(tok)>=3 and tok in t)
            if sc>bs: best,bs=x,sc
    return best if bs>=2 else None
def score_item(item,min_roi,min_profit,friction):
    title=item.get('title',''); low=title.lower(); target=match_target(title)
    price=float(item.get('price',0) or 0); shipping=float(item.get('shipping',0) or 0); buy=round(price+shipping,2)
    resale=target['resale'] if target else max(20,buy*1.45); bonus=0; risks=[]
    if target:
        for s in target['signals']:
            if s in low: bonus+=4
    for s in ['fake','replica','stain','hole','broken zip','kids','child']:
        if s in low: risks.append(s)
    resale=round(resale*(1+min(bonus,30)/100-max(0,len(risks)*0.08)),2)
    profit=round(resale-buy-resale*(friction/100),2); roi=round(profit/buy*100,1) if buy>0 else 0
    score=10+(30 if target else 0)+(20 if target and buy and buy<=target['great_buy'] else 0)+min(25,max(0,roi/8))+min(15,bonus/2)-min(25,len(risks)*8)
    if buy<=0: decision,rank='CHECK PRICE',2
    elif len(risks)>=2: decision,rank='SKIP / INSPECT',3
    elif profit>=min_profit and roi>=min_roi: decision,rank='BUY',0
    elif profit>0 and roi>=40: decision,rank='MAYBE',1
    else: decision,rank='SKIP',3
    return {**item,'matched_target':target['name'] if target else '','total_buy_eur':buy,'estimated_resale_eur':resale,'estimated_profit_eur':profit,'roi_pct':roi,'score':round(max(0,min(100,score)),1),'decision':decision,'decision_rank':rank,'risk':(', '.join(risks) if risks else (target['risk'] if target else 'Unknown'))}
def save_deals(rows):
    with db() as c:
        for d in rows:
            fp=hashlib.sha256(((d.get('url') or '')+'|'+d.get('title','')).encode()).hexdigest()
            c.execute('INSERT OR IGNORE INTO deals VALUES (?,?,?,?,?,?,?,?,?,?,?)',(fp,d.get('title',''),d.get('source',''),d.get('url',''),d.get('total_buy_eur',0),d.get('estimated_resale_eur',0),d.get('estimated_profit_eur',0),d.get('roi_pct',0),d.get('score',0),d.get('decision',''),int(time.time())))
def brave_search(q,count=10):
    key=os.getenv('BRAVE_SEARCH_API_KEY')
    if not key: raise RuntimeError('Add BRAVE_SEARCH_API_KEY in Railway Variables to enable live search.')
    r=requests.get('https://api.search.brave.com/res/v1/web/search',headers={'Accept':'application/json','X-Subscription-Token':key},params={'q':q,'count':min(count,20),'search_lang':'en'},timeout=20)
    r.raise_for_status(); return r.json().get('web',{}).get('results',[])
def indexed_search(query,region):
    clause=' OR '.join(f'site:{d}' for d in REGION_DOMAINS[region]); out=[]
    for x in brave_search(f'"{query}" ({clause})',12):
        u=x.get('url',''); out.append({'title':x.get('title',''),'source':f"Indexed: {urlparse(u).netloc.replace('www.','')}",'price':0,'shipping':0,'url':u})
    return out
def wholesaler_search(category,region):
    out=[]; seen=set()
    for q in [f'"{category}" wholesale {region}',f'"{category}" handpick warehouse {region}',f'"{category}" vintage kilo {region}',f'"{category}" job lot supplier {region}']:
        for x in brave_search(q,10):
            u=x.get('url','')
            if not u or u in seen: continue
            seen.add(u); out.append({'Supplier':x.get('title',''),'URL':u,'Description':x.get('description','')})
    return out[:25]
st.title('👖 Vintage Deal Finder EU'); st.caption('Finland/EU-focused sourcing tool — no eBay dependency.')
with st.sidebar:
    st.header('Deal rules'); min_roi=st.number_input('Minimum ROI %',0,1000,80,10); min_profit=st.number_input('Minimum profit €',0,1000,20,5); max_buy=st.number_input('Maximum item price €',1,1000,60,5); max_shipping=st.number_input('Maximum shipping to Finland €',0,200,12,1); friction=st.number_input('Selling friction %',0,50,12,1); region=st.selectbox('Preferred sourcing region',['Finland','EU','Nordics','Europe'])
t1,t2,t3,t4,t5=st.tabs(['🔥 Deal Finder','📥 Import','🏭 EU Wholesalers','📚 Pricebook','🕘 History'])
with t1:
    opts=sum([p['keywords'] for p in PRICEBOOK],[]); default=['levis 501 vintage','levis 517 vintage','diesel zathan','diesel zatiny','true religion ricky super t','miss sixty y2k','evisu no 2']; selected=st.multiselect('Searches',opts,default=[x for x in default if x in opts])
    if st.button('Search Europe',type='primary'):
        raw=[]
        try:
            for q in selected: raw+=indexed_search(q,region)
        except Exception as e: st.warning(str(e))
        if raw:
            scored=[score_item(x,min_roi,min_profit,friction) for x in raw]; save_deals(scored); st.dataframe(pd.DataFrame(scored).sort_values(['decision_rank','score'],ascending=[True,False]),use_container_width=True,hide_index=True)
with t2:
    st.write('Upload CSV columns: title,price,shipping,url,source.'); f=st.file_uploader('CSV file',type=['csv'])
    if f:
        df=pd.read_csv(f); raw=[]
        for _,r in df.iterrows():
            p=float(r.get('price',0) or 0); s=float(r.get('shipping',0) or 0)
            if p<=max_buy and s<=max_shipping: raw.append({'title':str(r.get('title','')),'price':p,'shipping':s,'url':str(r.get('url','')),'source':str(r.get('source','import'))})
        scored=[score_item(x,min_roi,min_profit,friction) for x in raw]; save_deals(scored); out=pd.DataFrame(scored).sort_values(['decision_rank','score'],ascending=[True,False]); st.dataframe(out,use_container_width=True,hide_index=True); st.download_button('Download scored deals',out.to_csv(index=False).encode(),file_name='scored_deals.csv')
with t3:
    cat=st.selectbox('Category',['Vintage denim',"Levi's",'Diesel','True Religion','Miss Sixty','Evisu','Y2K clothing','Vintage workwear','Vintage clothing']); reg=st.selectbox('Supplier region',['Finland','Nordics','EU','Europe'])
    if st.button('Find EU suppliers'):
        try: st.dataframe(pd.DataFrame(wholesaler_search(cat,reg)),use_container_width=True,hide_index=True)
        except Exception as e: st.warning(str(e))
with t4: st.dataframe(pd.DataFrame([{'Target':p['name'],'Typical resale €':p['resale'],'Strong buy ≤ €':p['great_buy'],'Counterfeit risk':p['risk'],'Signals':', '.join(p['signals'])} for p in PRICEBOOK]),use_container_width=True,hide_index=True)
with t5:
    with db() as c: rows=c.execute('SELECT title,source,url,buy,resale,profit,roi,score,decision,created FROM deals ORDER BY created DESC LIMIT 500').fetchall()
    if rows: st.dataframe(pd.DataFrame(rows,columns=['title','source','url','buy','resale','profit','roi','score','decision','created']),use_container_width=True,hide_index=True)
    else: st.info('No saved deals yet.')
st.caption('Live web search uses a search API and does not bypass marketplace anti-bot protections.')
