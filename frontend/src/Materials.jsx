import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { apiUrl } from './project.js';
import './materials.css';

function priceLabel(price) {
  if (!price) return 'See store for price';
  const currency = new Intl.NumberFormat(undefined, {
    style: 'currency', currency: price.currency, currencyDisplay: 'code',
  });
  return currency.format(price.amount / (10 ** currency.resolvedOptions().maximumFractionDigits));
}

const materialQuery = item => (item.search_query?.trim() || item.name.trim()).slice(0, 300);

function useCatalog(needed, enabled) {
  const [results, setResults] = useState(() => new Map());
  const requests = useRef(new Map());
  useEffect(() => {
    const active = requests.current;
    return () => {
      for (const request of active.values()) request.abort();
      active.clear();
    };
  }, []);
  useEffect(() => {
    const active = requests.current;
    const queries = new Set(enabled ? needed.map(materialQuery).filter(Boolean) : []);
    for (const [query, request] of active) {
      if (!queries.has(query)) {
        request.abort();
        active.delete(query);
      }
    }

    async function search(query, request) {
      let result;
      try {
        const response = await fetch(apiUrl('/api/shopping/search?' + new URLSearchParams({ q: query })), {
          signal: request.signal, cache: 'no-store',
        });
        if (!response.ok) throw new Error();
        const { products } = await response.json();
        if (!Array.isArray(products)) throw new Error();
        result = { products };
      } catch {
        result = { error: true };
      }
      if (!request.signal.aborted) {
        active.delete(query);
        setResults(previous => new Map(previous).set(query, result));
      }
    }

    // A guide can contain many supplies sharing the same catalog query.
    for (const query of queries) {
      if (active.size >= 2) break;
      if (results.has(query) || active.has(query)) continue;
      const request = new AbortController();
      active.set(query, request);
      search(query, request);
    }
  }, [needed, enabled, results]);
  return results;
}

function ShopLink({ query, name }) {
  const url = new URL('https://shop.app/search/results');
  url.searchParams.set('query', query);
  return <a className="shop-link" href={url.href} target="_blank" rel="noopener noreferrer"
    aria-label={'Search Shop by Shopify for ' + name}>Search Shop by Shopify <span aria-hidden="true">↗</span></a>;
}

function ProductCarousel({ products, name, query }) {
  const [index, setIndex] = useState(0);
  const slideId = useId();
  const count = products.length;
  const selected = Math.min(index, count - 1);
  const product = products[selected];
  const seller = product.seller || new URL(product.url || product.checkout_url).hostname;
  function move(offset) {
    return (selected + offset + count) % count;
  }
  function navigate(event) {
    const keys = { ArrowLeft: move(-1), ArrowRight: move(1), Home: 0, End: count - 1 };
    if (!Object.hasOwn(keys, event.key)) return;
    event.preventDefault();
    const next = keys[event.key];
    setIndex(next);
    if (event.target.hasAttribute('data-choice')) {
      event.currentTarget.querySelector('[data-choice="' + next + '"]').focus();
    }
  }

  return <div className="catalog-carousel" role="group" aria-roledescription={count > 1 ? 'carousel' : undefined}
    aria-label={'Shopify products for ' + name}>
    <div aria-live="polite" aria-atomic="true">
      <div className="catalog-offer" id={slideId} role="group" aria-roledescription={count > 1 ? 'slide' : undefined}
        aria-label={`Product ${selected + 1} of ${count}`}>
        {product.image_url && <img key={product.image_url} className="catalog-image" src={product.image_url} alt=""
          loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={event => { event.currentTarget.hidden = true; }} />}
        <div className="catalog-copy">
          <p className="catalog-seller">{seller}</p>
          {product.url ? <a className="catalog-title" href={product.url} target="_blank" rel="noopener noreferrer">
            {product.title} <span aria-hidden="true">↗</span></a> : <strong className="catalog-title">{product.title}</strong>}
          {product.variant && <p className="catalog-variant">{product.variant}</p>}
          <div className="catalog-buy">
            <span className={'catalog-price' + (product.price ? '' : ' unavailable')}>{priceLabel(product.price)}</span>
            {product.checkout_url && <a className="catalog-checkout" href={product.checkout_url} target="_blank" rel="noopener noreferrer"
              aria-label={'Checkout at the store for ' + product.title}>Checkout at store <span aria-hidden="true">↗</span></a>}
          </div>
        </div>
      </div>
    </div>
    <div className="catalog-footer">
      {count > 1 && <div className="catalog-controls" role="group" aria-label={'Browse products for ' + name} onKeyDown={navigate}>
        <button type="button" aria-label={'Previous product for ' + name} aria-controls={slideId} onClick={() => setIndex(move(-1))}>←</button>
        {products.map((offer, i) => <button type="button" className="catalog-dot" key={offer.checkout_url || offer.url}
          data-choice={i} aria-label={`Show product ${i + 1} of ${count} for ${name}`} aria-current={i === selected ? 'true' : undefined}
          aria-controls={slideId} onClick={() => setIndex(i)}><i aria-hidden="true" /></button>)}
        <button type="button" aria-label={'Next product for ' + name} aria-controls={slideId} onClick={() => setIndex(move(1))}>→</button>
      </div>}
      <ShopLink query={query} name={name} />
    </div>
  </div>;
}

function MaterialShopping({ item, enabled, result }) {
  const query = materialQuery(item);
  if (!query) return null;
  if (enabled && result?.products?.length) {
    return <ProductCarousel key={query} products={result.products} name={item.name} query={query} />;
  }
  return <div className="catalog-fallback">
    {enabled && <p className="catalog-status" role="status">{!result ? 'Searching Shopify…' : result.error
      ? 'Shopify’s catalog is unavailable.' : 'No matching products found.'}</p>}
    <ShopLink query={query} name={item.name} />
  </div>;
}

export default function Materials({ materials, checked, setChecked, catalogEnabled }) {
  const focusId = useRef(null);
  const section = useRef(null);
  const groups = useMemo(() => {
    const grouped = { 'Still needed': [], Gathered: [], Optional: [] };
    for (const item of materials) {
      const owned = Object.hasOwn(checked, item.id) ? checked[item.id] : ['OWNED_EXACT', 'OWNED_SUBSTITUTE'].includes(item.status);
      grouped[owned ? 'Gathered' : item.required === false ? 'Optional' : 'Still needed'].push(item);
    }
    return grouped;
  }, [materials, checked]);
  const results = useCatalog(groups['Still needed'], catalogEnabled);

  // Keep keyboard focus on the same supply as it moves between groups.
  useLayoutEffect(() => {
    if (focusId.current !== null) {
      const input = [...section.current.querySelectorAll('input')].find(el => el.value === focusId.current);
      input?.focus({ preventScroll: true });
      focusId.current = null;
    }
  }, [checked]);

  return <section className="supplies" aria-labelledby="supplies-title" ref={section}>
    <div className="supplies-header">
      <h2 id="supplies-title" tabIndex="-1">Materials &amp; tools</h2>
      <p className="meta" id="supply-count">{materials.length
        ? groups.Gathered.length + ' of ' + materials.length + ' gathered' : 'No supplies listed.'}</p>
    </div>
    <div id="materials">{Object.entries(groups).map(([group, items]) => !!items.length && <div className="supply-group" key={group}>
      <h3>{group}</h3>
      <ul className="supply-items">{items.map(item => <li key={item.id}>
        <label className="supply-check">
          <input type="checkbox" value={item.id} checked={group === 'Gathered'} onChange={event => {
            focusId.current = item.id;
            setChecked(item.id, event.target.checked);
          }} />
          <span>{item.name}<small>{[item.quantity, item.substitute_note].filter(Boolean).join(' · ')}</small></span>
        </label>
        {group === 'Still needed' && <MaterialShopping item={item} enabled={catalogEnabled} result={results.get(materialQuery(item))} />}
      </li>)}</ul>
    </div>)}</div>
  </section>;
}
