import os, re, sqlite3, hashlib, time
from urllib.parse import urlparse
import pandas as pd
import requests
import streamlit as st
from catalog import PRICEBOOK, CATEGORIES

st.set_page_config(page_title='Vintage Deal Finder EU', page_icon='👖', layout='wide')

REGION_DOMAINS={'Finland':['vinted.fi','tori.fi','huuto.net'],'Nordics':['vinted.fi','tori.fi','tradera.com','sellpy.fi','sellpy.se'],'EU':['vinted.fi','vinted.fr','vinted.de','vinted.nl','vinted.be','vinted.es','vinted.it','vinted.pt','tori.fi','tradera.com','sellpy.fi','sellpy.se','depop.com','grailed.com'],'Europe':['vinted.fi','vinted.fr','vinted.de','vinted.nl','vinted.be','vinted.es','vinted.it','vinted.pt','tori.fi','tradera.com','sellpy.fi','sellpy.se','depop.com','grailed.com']}
MARKETPLACE_DOMAINS={
    'Vinted':['vinted.fi','vinted.fr','vinted.de','vinted.nl','vinted.be','vinted.es','vinted.it','vinted.pt'],
    'Depop':['depop.com'],
    'Grailed':['grailed.com'],
    'Tradera':['tradera.com'],
    'Sellpy':['sellpy.fi','sellpy.se'],
    'Tori':['tori.fi'],
    'Facebook Marketplace':['facebook.com/marketplace']
}
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
    price=float(item.get('price',0) or 0); shipping=float(item.get('shipping',0) or 0)
    price_known=price>0
    buy=round(price+shipping,2) if price_known else 0
    resale=target['resale'] if target else 0
    bonus=0; risks=[]
    if target:
        for sig in target['signals']:
            if sig in low: bonus+=4
    for sig in ['fake','replica','stain','hole','broken zip','kids','child']:
        if sig in low: risks.append(sig)
    if resale>0:
        resale=round(resale*(1+min(bonus,30)/100-max(0,len(risks)*0.08)),2)
    profit=round(resale-buy-resale*(friction/100),2) if price_known and resale>0 else 0
    roi=round(profit/buy*100,1) if price_known and buy>0 and resale>0 else 0
    score=10+(30 if target else 0)+min(15,bonus/2)-min(25,len(risks)*8)
    if price_known and target and buy<=target['great_buy']: score+=20
    if price_known: score+=min(25,max(0,roi/8))
    if not price_known:
        decision,rank='CHECK PRICE',2
    elif not target:
        decision,rank='INSPECT',2
    elif len(risks)>=2:
        decision,rank='SKIP / INSPECT',3
    elif profit>=min_profit and roi>=min_roi:
        decision,rank='BUY',0
    elif profit>0 and roi>=40:
        decision,rank='MAYBE',1
    else:
        decision,rank='SKIP',3
    return {**item,'matched_target':target['name'] if target else '','buy_price_eur':price if price_known else None,'shipping_eur':shipping if shipping>0 else None,'total_buy_eur':buy if price_known else None,'estimated_resale_eur':resale if resale>0 else None,'estimated_profit_eur':profit if price_known and resale>0 else None,'roi_pct':roi if price_known and resale>0 else None,'score':round(max(0,min(100,score)),1),'decision':decision,'decision_rank':rank,'risk':(', '.join(risks) if risks else (target['risk'] if target else 'Unknown'))}
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
    params={'q':q,'count':min(count,20),'extra_snippets':True,'include_fetch_metadata':True}
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

def _to_price(value):
    try:
        if isinstance(value,(int,float)):
            v=float(value)
        else:
            raw=str(value).strip().replace('\xa0',' ')
            raw=re.sub(r'[^0-9,.-]','',raw).replace(',','.')
            if raw.count('.')>1:
                return 0
            v=float(raw)
        return round(v,2) if 2 <= v <= 1500 else 0
    except Exception:
        return 0

def extract_price_from_schemas(schemas):
    candidates=[]
    def walk(obj,currency_hint=''):
        if isinstance(obj,dict):
            currency=str(obj.get('priceCurrency',currency_hint) or currency_hint).upper()
            for key in ('price','lowPrice','highPrice'):
                if key in obj:
                    value=_to_price(obj.get(key))
                    if value:
                        priority=0 if currency in ('EUR','€','') else 1
                        candidates.append((priority,value,currency))
            for value in obj.values():
                walk(value,currency)
        elif isinstance(obj,list):
            for value in obj:
                walk(value,currency_hint)
    walk(schemas or [])
    if not candidates:
        return 0
    candidates.sort(key=lambda x:(x[0],x[1]))
    return candidates[0][1]

def extract_price_from_text(text):
    text=str(text or '')
    patterns=[
        r'"price"\s*:\s*"?([0-9]{1,4}(?:[.,][0-9]{1,2})?)',
        r'content=["\']([0-9]{1,4}(?:[.,][0-9]{1,2})?)["\'][^>]{0,80}(?:price|amount)',
        r'(?:€|EUR\s*)([0-9]{1,4}(?:[.,][0-9]{1,2})?)',
        r'([0-9]{1,4}(?:[.,][0-9]{1,2})?)\s*(?:€|EUR)'
    ]
    for pattern in patterns:
        for m in re.findall(pattern,text,re.I):
            try:
                value=float(str(m).replace(',','.'))
                if 2 <= value <= 1500:
                    return round(value,2)
            except Exception:
                pass
    return 0

def live_listing_check(url, fallback_text=''):
    result={'active_check':'UNVERIFIED','price':extract_price_from_text(fallback_text)}
    try:
        r=requests.get(
            url,
            headers={'User-Agent':'Mozilla/5.0 (compatible; VintageDealFinder/1.0)'},
            timeout=8,
            allow_redirects=True
        )
        if r.status_code in (404,410):
            result['active_check']='INACTIVE'
            return result
        if r.status_code==200:
            page=r.text[:1500000]
            low=page.lower()
            if any(term in low for term in INACTIVE_TERMS):
                result['active_check']='INACTIVE'
                return result
            page_price=extract_price_from_text(page)
            if page_price:
                result['price']=page_price
            result['active_check']='ACTIVE'
            return result
        if r.status_code in (401,403,429):
            result['active_check']='UNVERIFIED / SITE BLOCKED CHECK'
            return result
    except Exception:
        pass
    return result

def selected_domains(region,sources):
    region_domains=set(REGION_DOMAINS[region])
    out=[]
    for source in sources:
        for domain in MARKETPLACE_DOMAINS.get(source,[]):
            root=domain.split('/')[0]
            if source=='Facebook Marketplace' or root in region_domains or region in ('EU','Europe'):
                out.append(domain)
    return list(dict.fromkeys(out))

def indexed_search(query,region,sources):
    domains=selected_domains(region,sources)
    if not domains: return []
    clause=' OR '.join(f'site:{d}' for d in domains)
    negatives=' -sold -"sold out" -reserved -myyty -varattu -archived -expired -"not available"'
    out=[]; seen=set()

    def collect(results,label,strict_listing=True):
        for x in results:
            u=x.get('url','')
            if not u or u in seen or looks_inactive(x):
                continue
            if strict_listing and not looks_like_listing(u):
                continue
            snippets=[str(x.get('title','')),str(x.get('description',''))]
            snippets += [str(v) for v in (x.get('extra_snippets') or [])]
            snippet=' '.join(snippets)
            price=extract_price_from_schemas(x.get('schemas')) or extract_price_from_text(snippet)
            seen.add(u)
            host=urlparse(u).netloc.replace('www.','')
            out.append({
                'title':x.get('title','') or 'Untitled listing',
                'source':host,
                'price':price,
                'shipping':0,
                'url':u,
                'snippet':str(x.get('description','') or '')[:220],
                'active_check':label,
                'indexed_age':x.get('page_age') or x.get('age','')
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

def show_deals(df,min_roi,min_profit,friction,show_unknown=False):
    if df is None or df.empty:
        st.info('No results.')
        return
    priced=df[df['buy_price_eur'].notna()].copy() if 'buy_price_eur' in df.columns else pd.DataFrame()
    unknown=df[df['buy_price_eur'].isna()].copy() if 'buy_price_eur' in df.columns else df.copy()

    if not priced.empty:
        st.success(f'{len(priced)} tuloksessa ostohinta löytyi automaattisesti — ROI on laskettu näille.')
    else:
        st.warning('Tästä hausta ei löytynyt yhtään tulosta, jossa ostohinta olisi mukana hakudatassa. Näytän ilmoitukset manuaalista hintatarkistusta varten.')

    display_parts=[]
    if not priced.empty:
        display_parts.append(priced)
    if show_unknown or priced.empty:
        display_parts.append(unknown.head(20))
    display=pd.concat(display_parts,ignore_index=True) if display_parts else pd.DataFrame()

    for idx,row in display.head(30).iterrows():
        title=str(row.get('title') or 'Untitled listing')
        source=str(row.get('source') or '')
        auto_price=row.get('buy_price_eur')
        resale=row.get('estimated_resale_eur')
        url=str(row.get('url') or '')
        snippet=str(row.get('snippet') or '')
        active=str(row.get('active_check') or '')
        risk=str(row.get('risk') or '')

        with st.container(border=True):
            st.markdown(f'### {title}')
            st.caption(f'{source}  •  {active}')
            if snippet:
                st.write(snippet)

            price_value=None
            if pd.notna(auto_price) and auto_price not in (None,''):
                price_value=float(auto_price)
                st.success(f'Automaattisesti löydetty ostohinta: **{price_value:.2f} €**')
            else:
                st.warning('Ostohinta ei tullut hakudatassa mukana.')
                manual=st.number_input(
                    'Syötä ilmoituksen ostohinta (€)',
                    min_value=0.0,
                    max_value=2000.0,
                    value=0.0,
                    step=1.0,
                    key=f'manual_price_{idx}_{hash(url)}'
                )
                if manual>0:
                    price_value=float(manual)

            if price_value and pd.notna(resale) and resale not in (None,''):
                resale_value=float(resale)
                profit=round(resale_value-price_value-resale_value*(friction/100),2)
                roi=round(profit/price_value*100,1) if price_value>0 else 0
                if profit>=min_profit and roi>=min_roi:
                    decision='BUY'
                elif profit>0 and roi>=40:
                    decision='MAYBE'
                else:
                    decision='SKIP'
                c1,c2=st.columns(2)
                with c1:
                    st.metric('Ostohinta',f'{price_value:.2f} €')
                    st.metric('Arvioitu jälleenmyynti',f'{resale_value:.2f} €')
                with c2:
                    st.metric('Arvioitu voitto',f'{profit:.2f} €')
                    st.metric('ROI',f'{roi:.0f} %')
                st.write(f'**Arvio:** {decision}   |   **Riski:** {risk}')
            else:
                c1,c2=st.columns(2)
                with c1:
                    st.metric('Ostohinta','Ei saatavilla')
                    st.metric('Arvioitu jälleenmyynti',f'{float(resale):.2f} €' if pd.notna(resale) and resale not in (None,'') else '—')
                with c2:
                    st.metric('Arvioitu voitto','—')
                    st.metric('ROI','—')
                st.write(f'**Arvio:** CHECK PRICE   |   **Riski:** {risk}')

            if url.startswith('http'):
                st.link_button('Avaa ilmoitus ↗',url,width='stretch')

    if not unknown.empty and not show_unknown and not priced.empty:
        st.info(f'{len(unknown)} muuta tulosta piilotettiin, koska niistä ei löytynyt ostohintaa. Laita “Näytä myös ilman automaattista hintaa” päälle, jos haluat tarkistaa ne käsin.')

    with st.expander('Näytä tekninen taulukko'):
        view=df.copy()
        config={}
        if 'url' in view.columns:
            view=view.rename(columns={'url':'Open listing'})
            config['Open listing']=st.column_config.LinkColumn('Open listing',display_text='Open ↗')
        preferred=['title','source','buy_price_eur','estimated_resale_eur','estimated_profit_eur','roi_pct','decision','active_check','Open listing']
        cols=[c for c in preferred if c in view.columns]+[c for c in view.columns if c not in preferred]
        st.dataframe(view[cols],width='stretch',hide_index=True,column_config=config)

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
    selected=st.multiselect('Searches',opts,default=suggested[:4])
    sources=st.multiselect(
        'Marketplaces',
        list(MARKETPLACE_DOMAINS.keys()),
        default=['Vinted','Depop','Grailed','Tradera','Sellpy']
    )
    show_unknown=st.toggle('Näytä myös ilman automaattista hintaa',value=False)
    st.caption(f'{len(filtered)} resale targets. ROI näytetään vain, kun ostohinta on oikeasti tiedossa. Jos hinta puuttuu, voit syöttää sen korttiin käsin ja ROI lasketaan heti.')
    if st.button('Search Europe',type='primary'):
        raw=[]
        chosen=selected[:6]
        if len(selected)>6:
            st.info('Searching the first 6 selected terms to keep the search fast.')
        try:
            with st.spinner('Searching active-looking listings...'):
                for q in chosen:
                    raw+=indexed_search(q,region,sources)
        except requests.HTTPError as e:
            code=getattr(e.response,'status_code',None)
            if code==429:
                st.warning('Search API rate limit reached. Try again in a moment or select fewer searches.')
            else:
                st.warning(f'Search service error: {e}')
        except Exception as e:
            st.warning(f'Search error: {e}')
        if raw:
            scored=[score_item(x,min_roi,min_profit,friction) for x in raw]
            save_deals(scored)
            show_deals(pd.DataFrame(scored).sort_values(['decision_rank','score'],ascending=[True,False]),min_roi,min_profit,friction,show_unknown)
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
        show_deals(out,min_roi,min_profit,friction,True)
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
    if rows: st.dataframe(pd.DataFrame(rows,columns=['title','source','url','buy','resale','profit','roi','score','decision','created']),width='stretch',hide_index=True)
    else: st.info('No saved deals yet.')
st.caption('Live search uses public indexed marketplace pages. It does not bypass marketplace anti-bot protections. Facebook Marketplace coverage can be limited because many listings are not publicly indexed.')
