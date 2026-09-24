import os, re, sqlite3, hashlib, time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
import pandas as pd
import requests
import streamlit as st
from catalog import PRICEBOOK, CATEGORIES
from listing_data import indexed_inactive, page_details, schema_offer, snippet_price

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
            if not d.get('total_buy_eur') or not d.get('estimated_resale_eur'):
                continue
            fp=hashlib.sha256(((d.get('url') or '')+'|'+d.get('title','')).encode()).hexdigest()
            c.execute('INSERT OR REPLACE INTO deals VALUES (?,?,?,?,?,?,?,?,?,?,?)',(fp,d.get('title',''),d.get('source',''),d.get('url',''),d.get('total_buy_eur',0),d.get('estimated_resale_eur',0),d.get('estimated_profit_eur',0),d.get('roi_pct',0),d.get('score',0),d.get('decision',''),int(time.time())))
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
        return '/recommerce/forsale/item/' in path
    return False

def enrich_listing(item):
    """Inspect a bounded number of pages in parallel; keep unverified status explicit."""
    item=item.copy()
    try:
        response=requests.get(
            item['url'],
            headers={'User-Agent':'Mozilla/5.0'},
            timeout=(2,3),
            allow_redirects=False
        )
        if response.status_code in (404,410):
            return None
        if response.status_code==200:
            page_price,inactive=page_details(response.text,item['url'])
            if inactive:
                return None
            if page_price is not None:
                item['price']=page_price
                item['price_source']='ilmoitussivu'
                item['active_check']='Sivu aukeaa · tarkista saatavuus'
    except requests.RequestException:
        pass
    return item

def enrich_listings(items,limit=12):
    items=sorted(items,key=lambda x: x.get('price') is None)
    with ThreadPoolExecutor(max_workers=8) as pool:
        checked=list(pool.map(enrich_listing,items[:limit]))
    return [x for x in checked if x is not None]

def selected_domains(region,sources):
    region_domains=set(REGION_DOMAINS[region])
    out=[]
    for source in sources:
        for domain in MARKETPLACE_DOMAINS.get(source,[]):
            root=domain.split('/')[0]
            if source=='Facebook Marketplace' or root in region_domains or region in ('EU','Europe'):
                out.append(domain)
    return list(dict.fromkeys(out))

def allowed_domain(url,domains):
    parsed=urlparse(url)
    host=(parsed.hostname or '').lower().removeprefix('www.')
    return parsed.scheme=='https' and any(
        host==d.split('/')[0] and (not '/' in d or parsed.path.startswith('/'+d.split('/',1)[1]))
        for d in domains
    )

def indexed_search(query,region,sources):
    domains=selected_domains(region,sources)
    if not domains: return []
    clause=' OR '.join(f'site:{d}' for d in domains)
    negatives=' -sold -reserved -myyty -varattu -vendido -vendu -eliminado -expired'
    out=[]; seen=set()

    def collect(results):
        for x in results:
            u=x.get('url','')
            if not u or u in seen or not allowed_domain(u,domains) or indexed_inactive(x):
                continue
            if not looks_like_listing(u):
                continue
            snippets=[str(x.get('title','')),str(x.get('description',''))]
            snippets += [str(v) for v in (x.get('extra_snippets') or [])]
            snippet=' '.join(snippets)
            price,inactive=schema_offer(x.get('schemas'),u)
            if inactive:
                continue
            price=price or snippet_price(' '.join(snippets[:2]))
            seen.add(u)
            host=urlparse(u).netloc.replace('www.','')
            out.append({
                'title':x.get('title','') or 'Untitled listing',
                'source':host,
                'price':price,
                'price_source':'hakutulos' if price else '',
                'shipping':0,
                'url':u,
                'snippet':str(x.get('description','') or '')[:220],
                'active_check':'Hakutulos · tarkista saatavuus',
                'indexed_age':x.get('page_age') or x.get('age','')
            })

    # Search specific marketplaces first so the search engine cannot ignore a long OR clause.
    priorities=['vinted.fi','tori.fi','sellpy.fi','depop.com','grailed.com']
    focused=[d for d in priorities if d in domains][:2]
    if not focused:
        focused=domains[:2]
    for domain in focused:
        collect(brave_search(f'{query} € site:{domain} {negatives}',20,freshness='pm'))

    # Broaden once for listings whose indexed snippet has no currency sign.
    if len(out) < 4:
        collect(brave_search(f'{query} ({clause}) {negatives}',20,freshness=None))

    return out
def wholesaler_search(category,region):
    out=[]; seen=set()
    for q in [f'"{category}" wholesale {region}',f'"{category}" handpick warehouse {region}',f'"{category}" vintage kilo {region}',f'"{category}" job lot supplier {region}']:
        for x in brave_search(q,10):
            u=x.get('url','')
            if not u or u in seen: continue
            seen.add(u); out.append({'Supplier':x.get('title',''),'URL':u,'Description':x.get('description','')})
    return out[:25]

def show_deals(df,min_roi,min_profit,friction):
    if df is None or df.empty:
        st.info('Ei hintatiedollisia ilmoituksia tästä hausta. Kokeile toista mallia tai markkinapaikkaa.')
        return
    display=df[df['buy_price_eur'].notna() & df['estimated_resale_eur'].notna()].copy()
    omitted=len(df)-len(display)
    if display.empty:
        st.info(f'Ei ilmoituksia, joissa sekä ostohinta että jälleenmyyntiarvio ovat tiedossa. {omitted} puutteellista tulosta jätettiin pois.')
        return
    st.success(f'{len(display)} hintatiedollista ilmoitusta · jokaisessa näkyy laskelma.')
    if omitted:
        st.caption(f'{omitted} tulosta jätettiin pois puuttuvan hinnan tai jälleenmyyntiarvion takia.')

    for idx,row in display.head(30).iterrows():
        title=str(row.get('title') or 'Untitled listing')
        source=str(row.get('source') or '')
        price=float(row['buy_price_eur'])
        resale=float(row['estimated_resale_eur'])
        url=str(row.get('url') or '')
        snippet=str(row.get('snippet') or '')
        active=str(row.get('active_check') or '')
        risk=str(row.get('risk') or '')

        with st.container(border=True):
            st.markdown(f'### {title}')
            st.caption(f'{source}  •  {active}')
            if snippet:
                st.write(snippet)
            source_label='ilmoitussivulta' if row.get('price_source')=='ilmoitussivu' else 'hakutuloksesta'
            st.caption(f'Tuotehinta {source_label}; tarkista hinta ja saatavuus ennen ostoa.')
            base_shipping=row.get('shipping_eur')
            shipping=st.number_input(
                'Toimitus ja ostajan kulut (€)', min_value=0.0, max_value=2000.0,
                value=float(base_shipping) if pd.notna(base_shipping) else 0.0, step=1.0,
                key='shipping_'+hashlib.sha256(url.encode()).hexdigest()[:16]
            )
            total=price+shipping
            profit=round(resale-total-resale*(friction/100),2)
            roi=round(profit/total*100,1)
            decision='BUY' if profit>=min_profit and roi>=min_roi else ('MAYBE' if profit>0 and roi>=40 else 'SKIP')
            display.at[idx,'shipping_eur']=shipping
            display.at[idx,'total_buy_eur']=total
            display.at[idx,'estimated_profit_eur']=profit
            display.at[idx,'roi_pct']=roi
            display.at[idx,'decision']=decision
            c1,c2=st.columns(2)
            with c1:
                st.metric('Ilmoituksen hinta',f'{price:.2f} €')
                st.metric('Arvioitu jälleenmyynti',f'{resale:.2f} €')
                st.metric('Oston kokonaiskulu',f'{total:.2f} €')
            with c2:
                st.metric('Arvioitu voitto',f'{profit:.2f} €')
                st.metric('ROI',f'{roi:.1f} %')
                st.write(f'**Arvio:** {decision}   |   **Riski:** {risk}')
            st.caption(f'Voittoarviossa on mukana {friction} % myyntikuluja. Jälleenmyyntihinta on arvio, ei toteutunut kauppa.')

            if url.startswith('http'):
                st.link_button('Avaa ilmoitus ↗',url,width='stretch')

    with st.expander('Näytä tekninen taulukko'):
        view=display.copy()
        config={}
        if 'url' in view.columns:
            view=view.rename(columns={'url':'Open listing'})
            config['Open listing']=st.column_config.LinkColumn('Open listing',display_text='Open ↗')
        preferred=['title','source','buy_price_eur','estimated_resale_eur','estimated_profit_eur','roi_pct','decision','active_check','Open listing']
        cols=[c for c in preferred if c in view.columns]+[c for c in view.columns if c not in preferred]
        st.dataframe(view[cols],width='stretch',hide_index=True,column_config=config)
    return display

def show_suppliers(df):
    view=df.copy()
    config={}
    if 'URL' in view.columns:
        view=view.rename(columns={'URL':'Open website'})
        config['Open website']=st.column_config.LinkColumn('Open website',display_text='Open ↗')
    st.dataframe(view,width='stretch',hide_index=True,column_config=config)
st.title('👖 Vintage Deal Finder EU'); st.caption(f'Finland/EU sourcing across {len(PRICEBOOK)} high-interest vintage targets — denim, workwear, sportswear, Y2K, outdoor, racing, streetwear and archive.')
with st.sidebar:
    st.header('Deal rules'); min_roi=st.number_input('Minimum ROI %',0,1000,80,10); min_profit=st.number_input('Minimum profit €',0,1000,20,5); max_buy=st.number_input('Maximum item price €',1,1000,60,5); max_shipping=st.number_input('Maximum shipping to Finland €',0,200,12,1); friction=st.number_input('Selling friction %',0,50,12,1); region=st.selectbox('Preferred sourcing region',['Finland','EU','Nordics','Europe'],index=1)
t1,t2,t3,t4,t5=st.tabs(['🔥 Deal Finder','📥 Import','🏭 EU Wholesalers','📚 Pricebook','🕘 History'])
with t1:
    search_category=st.selectbox('Vintage category',CATEGORIES,key='deal_category')
    filtered=PRICEBOOK if search_category=='All' else [p for p in PRICEBOOK if p['category']==search_category]
    opts=sum([p['keywords'] for p in filtered],[])
    suggested=[p['keywords'][0] for p in filtered[:10]]
    selected=st.multiselect('Searches',opts,default=suggested[:1])
    sources=st.multiselect(
        'Marketplaces',
        list(MARKETPLACE_DOMAINS.keys()),
        default=['Vinted','Depop','Grailed','Tradera','Sellpy','Tori']
    )
    st.caption(f'{len(filtered)} jälleenmyyntikohdetta. Vain ilmoitukset, joiden euromääräinen hinta ja jälleenmyyntiarvio löytyvät, näytetään.')
    if st.button('Search Europe',type='primary'):
        st.session_state.pop('live_results',None)
        raw=[]
        chosen=selected[:6]
        if len(selected)>6:
            st.info('Searching the first 6 selected terms to keep the search fast.')
        try:
            with st.spinner('Searching active-looking listings...'):
                for q in chosen:
                    raw+=indexed_search(q,region,sources)
                raw=enrich_listings(raw)
            st.session_state['live_results']=raw
            st.session_state['live_query']=', '.join(chosen)
            st.session_state.pop('live_error',None)
        except requests.HTTPError as e:
            code=getattr(e.response,'status_code',None)
            if code==429:
                st.session_state['live_error']='Hakupalvelun käyttöraja täyttyi. Kokeile hetken päästä uudelleen.'
            else:
                st.session_state['live_error']=f'Hakupalvelun virhe: {e}'
        except Exception as e:
            st.session_state['live_error']=f'Haku epäonnistui: {e}'
    if st.session_state.get('live_error'):
        st.warning(st.session_state['live_error'])
    elif 'live_results' in st.session_state:
        st.caption(f'Haku: {st.session_state.get("live_query","")}')
        raw=st.session_state['live_results']
        scored=[score_item(x,min_roi,min_profit,friction) for x in raw if x.get('price') and x['price']<=max_buy]
        priced=[x for x in scored if x['estimated_resale_eur'] is not None]
        if priced:
            displayed=show_deals(pd.DataFrame(priced).sort_values(['decision_rank','score'],ascending=[True,False]),min_roi,min_profit,friction)
            if displayed is not None:
                save_deals(displayed.to_dict('records'))
        else:
            st.info('Hausta ei löytynyt hintatiedollista ilmoitusta nykyisellä enimmäishinnalla. Kokeile toista mallia tai laajenna aluetta.')
with t2:
    st.write('Upload CSV columns: title,price,shipping,url,source.'); f=st.file_uploader('CSV file',type=['csv'])
    if f:
        df=pd.read_csv(f); raw=[]
        for _,r in df.iterrows():
            p=float(r.get('price',0) or 0); s=float(r.get('shipping',0) or 0)
            if 0<p<=max_buy and s<=max_shipping: raw.append({'title':str(r.get('title','')),'price':p,'shipping':s,'url':str(r.get('url','')),'source':str(r.get('source','import'))})
        scored=[score_item(x,min_roi,min_profit,friction) for x in raw]
        if scored:
            out=pd.DataFrame(scored).sort_values(['decision_rank','score'],ascending=[True,False])
            displayed=show_deals(out,min_roi,min_profit,friction)
            if displayed is not None:
                save_deals(displayed.to_dict('records'))
                out=displayed
            st.download_button('Download scored deals',out.to_csv(index=False).encode(),file_name='scored_deals.csv')
        else:
            st.info('Tiedostossa ei ole enimmäishintaan sopivia tuotteita.')
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
    with db() as c: rows=c.execute('SELECT title,source,url,buy,resale,profit,roi,score,decision,created FROM deals WHERE buy>0 AND resale>0 ORDER BY created DESC LIMIT 500').fetchall()
    if rows: st.dataframe(pd.DataFrame(rows,columns=['title','source','url','buy','resale','profit','roi','score','decision','created']),width='stretch',hide_index=True)
    else: st.info('No saved deals yet.')
st.caption('Live search uses public indexed marketplace pages. It does not bypass marketplace anti-bot protections. Facebook Marketplace coverage can be limited because many listings are not publicly indexed.')
