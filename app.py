import os, re, sqlite3, hashlib, time
from urllib.parse import urlparse
import pandas as pd
import requests
import streamlit as st
from catalog import PRICEBOOK, CATEGORIES

st.set_page_config(page_title='Vintage Deal Finder EU', page_icon='👖', layout='wide')

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
INACTIVE_TERMS = [
    'sold','sold out','item sold','already sold','reserved','reservation',
    'myyty','myyty loppuun','varattu','poistettu','deleted','removed',
    'archived','archive listing','not available','unavailable','expired',
    'ended','listing ended','no longer available'
]

def brave_search(q,count=10,freshness=None):
    key=os.getenv('BRAVE_SEARCH_API_KEY')
    if not key: raise RuntimeError('Add BRAVE_SEARCH_API_KEY in Railway Variables to enable live search.')
    params={'q':q,'count':min(count,20),'search_lang':'en'}
    if freshness:
        params['freshness']=freshness
    r=requests.get(
        'https://api.search.brave.com/res/v1/web/search',
        headers={'Accept':'application/json','X-Subscription-Token':key},
        params=params,
        timeout=20
    )
    r.raise_for_status()
    return r.json().get('web',{}).get('results',[])

def looks_inactive(result):
    text=' '.join([
        str(result.get('title','')),
        str(result.get('description','')),
        str(result.get('url',''))
    ]).lower()
    return any(term in text for term in INACTIVE_TERMS)

def looks_like_listing(url):
    low=(url or '').lower()
    host=urlparse(low).netloc.replace('www.','')
    path=urlparse(low).path
    if 'vinted.' in host:
        return '/items/' in path
    if 'grailed.com' in host:
        return '/listings/' in path
    if 'depop.com' in host:
        return '/products/' in path
    if 'tradera.com' in host:
        return '/item/' in path or '/listing/' in path
    if 'sellpy.' in host:
        return '/item/' in path or '/product/' in path
    if 'tori.fi' in host:
        return '/recommerce/forsale/item/' in path or '/recommerce/forsale/search' not in low
    return True

def indexed_search(query,region):
    clause=' OR '.join(f'site:{d}' for d in REGION_DOMAINS[region])
    negatives=' -sold -"sold out" -reserved -myyty -varattu -archived -expired -"not available"'
    out=[]; seen=set()

    def collect(results,label,strict_listing=True):
        for x in results:
            u=x.get('url','')
            if not u or u in seen or looks_inactive(x):
                continue
            if strict_listing and not looks_like_listing(u):
                continue
            seen.add(u)
            out.append({
                'title':x.get('title',''),
                'source':f"Indexed: {urlparse(u).netloc.replace('www.','')}",
                'price':0,
                'shipping':0,
                'url':u,
                'active_check':label,
                'indexed_age':x.get('age','')
            })

    # Prefer recent listing pages first.
    collect(brave_search(f'"{query}" ({clause}) {negatives}',20,freshness='pm'),'RECENT / NO SOLD SIGNAL',True)

    # If too few results are found, broaden the index automatically.
    if len(out) < 4:
        collect(brave_search(f'"{query}" ({clause}) {negatives}',20,freshness=None),'NO SOLD SIGNAL',True)

    return out
def wholesaler_search(category,region):
    out=[]; seen=set()
    for q in [f'"{category}" wholesale {region}',f'"{category}" handpick warehouse {region}',f'"{category}" vintage kilo {region}',f'"{category}" job lot supplier {region}']:
        for x in brave_search(q,10):
            u=x.get('url','')
            if not u or u in seen: continue
            seen.add(u); out.append({'Supplier':x.get('title',''),'URL':u,'Description':x.get('description','')})
    return out[:25]

def show_deals(df):
    view=df.copy()
    config={}
    if 'url' in view.columns:
        view=view.rename(columns={'url':'Open listing'})
        config['Open listing']=st.column_config.LinkColumn('Open listing',display_text='Open ↗')
    st.dataframe(view,width='stretch',hide_index=True,column_config=config)

def show_suppliers(df):
    view=df.copy()
    config={}
    if 'URL' in view.columns:
        view=view.rename(columns={'URL':'Open website'})
        config['Open website']=st.column_config.LinkColumn('Open website',display_text='Open ↗')
    st.dataframe(view,width='stretch',hide_index=True,column_config=config)
st.title('👖 Vintage Deal Finder EU'); st.caption(f'Finland/EU sourcing across {len(PRICEBOOK)} high-interest vintage targets — denim, workwear, sportswear, Y2K, outdoor, racing, streetwear and archive.')
with st.sidebar:
    st.header('Deal rules'); min_roi=st.number_input('Minimum ROI %',0,1000,80,10); min_profit=st.number_input('Minimum profit €',0,1000,20,5); max_buy=st.number_input('Maximum item price €',1,1000,60,5); max_shipping=st.number_input('Maximum shipping to Finland €',0,200,12,1); friction=st.number_input('Selling friction %',0,50,12,1); region=st.selectbox('Preferred sourcing region',['Finland','EU','Nordics','Europe'])
t1,t2,t3,t4,t5=st.tabs(['🔥 Deal Finder','📥 Import','🏭 EU Wholesalers','📚 Pricebook','🕘 History'])
with t1:
    search_category=st.selectbox('Vintage category',CATEGORIES,key='deal_category')
    filtered=PRICEBOOK if search_category=='All' else [p for p in PRICEBOOK if p['category']==search_category]
    opts=sum([p['keywords'] for p in filtered],[])
    suggested=[p['keywords'][0] for p in filtered[:10]]
    selected=st.multiselect('Searches',opts,default=suggested[:8])
    st.caption(f'{len(filtered)} resale targets in this category. Search prefers recent listings and automatically widens if needed while filtering sold/reserved/expired signals.')
    if st.button('Search Europe',type='primary'):
        raw=[]
        try:
            for q in selected: raw+=indexed_search(q,region)
        except Exception as e: st.warning(str(e))
        if raw:
            scored=[score_item(x,min_roi,min_profit,friction) for x in raw]
            save_deals(scored)
            show_deals(pd.DataFrame(scored).sort_values(['decision_rank','score'],ascending=[True,False]))
        else:
            st.info('No matching active-looking listings found. Try fewer selected searches, another category, or switch Preferred sourcing region to EU/Europe.')
with t2:
    st.write('Upload CSV columns: title,price,shipping,url,source.'); f=st.file_uploader('CSV file',type=['csv'])
    if f:
        df=pd.read_csv(f); raw=[]
        for _,r in df.iterrows():
            p=float(r.get('price',0) or 0); s=float(r.get('shipping',0) or 0)
            if p<=max_buy and s<=max_shipping: raw.append({'title':str(r.get('title','')),'price':p,'shipping':s,'url':str(r.get('url','')),'source':str(r.get('source','import'))})
        scored=[score_item(x,min_roi,min_profit,friction) for x in raw]
        save_deals(scored)
        out=pd.DataFrame(scored).sort_values(['decision_rank','score'],ascending=[True,False])
        show_deals(out)
        st.download_button('Download scored deals',out.to_csv(index=False).encode(),file_name='scored_deals.csv')
with t3:
    cat=st.selectbox('Category',['Vintage clothing']+CATEGORIES[1:]); reg=st.selectbox('Supplier region',['Finland','Nordics','EU','Europe'])
    if st.button('Find EU suppliers'):
        try: show_suppliers(pd.DataFrame(wholesaler_search(cat,reg)))
        except Exception as e: st.warning(str(e))
with t4:
    price_category=st.selectbox('Filter pricebook',CATEGORIES,key='price_category')
    price_rows=PRICEBOOK if price_category=='All' else [p for p in PRICEBOOK if p['category']==price_category]
    st.dataframe(pd.DataFrame([{'Category':p['category'],'Target':p['name'],'Typical resale €':p['resale'],'Strong buy ≤ €':p['great_buy'],'Counterfeit risk':p['risk'],'Signals':', '.join(p['signals'])} for p in price_rows]),width='stretch',hide_index=True)
    st.caption('Starter estimates only — verify condition, authenticity and recent sold comps before buying.')
with t5:
    with db() as c: rows=c.execute('SELECT title,source,url,buy,resale,profit,roi,score,decision,created FROM deals ORDER BY created DESC LIMIT 500').fetchall()
    if rows: show_deals(pd.DataFrame(rows,columns=['title','source','url','buy','resale','profit','roi','score','decision','created']))
    else: st.info('No saved deals yet.')
st.caption('Live web search uses a search API and does not bypass marketplace anti-bot protections.')
