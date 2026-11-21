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

function useCatalog(queries) {
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
    // A lookup already under way can finish while its material moves into Gathered.
    for (const query of active.keys()) if (results.has(query)) active.delete(query);

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
        // Keep the slot until React commits this result, including during intervening renders.
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
  }, [queries, results]);
  function retry(query) {
    setResults(previous => {
      if (!previous.has(query)) return previous;
      const next = new Map(previous);
      next.delete(query);
      return next;
    });
  }
  return { results, retry };
}

function ProductCarousel({ products, name }) {
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

  const description = <>
    <span className="catalog-title">{product.title}{product.url && <span aria-hidden="true"> ↗</span>}</span>
    <span className="catalog-meta" id={slideId + '-details'}><span className="catalog-price">{priceLabel(product.price)}</span>
      {' · '}{seller}{product.variant && ' · ' + product.variant}</span>
  </>;

  return <div className="catalog-carousel" role="group" aria-roledescription={count > 1 ? 'carousel' : undefined}
    aria-label={'Shopify products for ' + name}>
    <div aria-live="polite" aria-atomic="true">
      <div className="catalog-offer" id={slideId} role="group" aria-roledescription={count > 1 ? 'slide' : undefined}
        aria-label={`Product ${selected + 1} of ${count}`}>
        {product.image_url && <img key={product.image_url} className="catalog-image" src={product.image_url} alt=""
          loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={event => { event.currentTarget.hidden = true; }} />}
        <div className="catalog-copy">
          {product.url ? <a className="catalog-product" href={product.url} target="_blank" rel="noopener noreferrer"
            aria-label={product.title} aria-describedby={slideId + '-details'}>{description}</a> : description}
          {product.checkout_url && <a className="catalog-checkout" href={product.checkout_url} target="_blank" rel="noopener noreferrer"
            aria-label={'Checkout at the store for ' + product.title}>Checkout at store <span aria-hidden="true">↗</span></a>}
        </div>
      </div>
    </div>
    {count > 1 && <div className="catalog-controls" role="group" aria-label={'Browse products for ' + name} onKeyDown={navigate}>
      <button type="button" aria-label={'Previous product for ' + name} aria-controls={slideId} onClick={() => setIndex(move(-1))}>←</button>
      {products.map((offer, i) => <button type="button" className="catalog-dot" key={offer.checkout_url || offer.url}
        data-choice={i} aria-label={`Show product ${i + 1} of ${count} for ${name}`} aria-current={i === selected ? 'true' : undefined}
        aria-controls={slideId} onClick={() => setIndex(i)}><i aria-hidden="true" /></button>)}
      <button type="button" aria-label={'Next product for ' + name} aria-controls={slideId} onClick={() => setIndex(move(1))}>→</button>
    </div>}
  </div>;
}

function MaterialShopping({ name, searching, result, onRetry }) {
  if (result?.products?.length) {
    return <ProductCarousel products={result.products} name={name} />;
  }
  const pending = !result && searching;
  return <div className="catalog-message">
    <p className="catalog-status" role="status">{pending ? 'Searching Shopify…' : result?.products
      ? 'No matching Shopify products found.' : 'Shopify’s catalog is unavailable.'}</p>
    {!pending && <button className="catalog-retry" type="button" onClick={onRetry}
      aria-label={'Retry Shopify search for ' + name}>Try again</button>}
  </div>;
}

function SupplyRow({ item, gathered, expanded, result, searching, onToggle, onCheck, onRetry }) {
  const panelId = useId();
  const offerCount = result?.products?.length;
  const offerLabel = offerCount == null ? 'offers' : offerCount + (offerCount === 1 ? ' offer' : ' offers');

  return <li className="supply-row">
    <label className="supply-check">
      <input type="checkbox" value={item.id} checked={gathered} onChange={event => onCheck(event.target.checked)} />
      <span className="supply-description">
        <span className="supply-name">{item.name}</span>
        {item.quantity && <span className="supply-quantity">{item.quantity}</span>}
        {item.substitute_note && <small className="supply-note">{item.substitute_note}</small>}
      </span>
    </label>
    {materialQuery(item) && <>
      <button className="catalog-toggle" type="button" aria-expanded={expanded} aria-controls={panelId}
        aria-label={`Shopify ${offerLabel} for ${item.name}`} onClick={onToggle}>
        Shopify{offerCount != null && <span className="catalog-count">{offerCount}</span>}
      </button>
      <div className="catalog-panel" id={panelId} hidden={!expanded}>
        {expanded && <MaterialShopping name={item.name} result={result} searching={searching} onRetry={onRetry} />}
      </div>
    </>}
  </li>;
}

export default function Materials({ materials, checked, setChecked, catalogEnabled }) {
  const focusId = useRef(null);
  const section = useRef(null);
  const [requested, setRequested] = useState(() => new Set());
  const [expandedId, setExpandedId] = useState(null);
  const groups = useMemo(() => {
    const grouped = { 'Still needed': [], Gathered: [], Optional: [] };
    for (const item of materials) {
      const owned = Object.hasOwn(checked, item.id) ? checked[item.id] : ['OWNED_EXACT', 'OWNED_SUBSTITUTE'].includes(item.status);
      grouped[owned ? 'Gathered' : item.required === false ? 'Optional' : 'Still needed'].push(item);
    }
    return grouped;
  }, [materials, checked]);
  const queries = useMemo(() => {
    const queries = new Set(requested);
    if (catalogEnabled) {
      for (const item of [...groups['Still needed'], ...groups.Optional]) {
        const query = materialQuery(item);
        if (query) queries.add(query);
      }
    }
    return queries;
  }, [groups, requested, catalogEnabled]);
  const { results, retry } = useCatalog(queries);
  const visibleGroups = Object.entries(groups).filter(([, items]) => items.length);

  function request(query) {
    setRequested(previous => previous.has(query) ? previous : new Set(previous).add(query));
  }

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
    <div id="materials">{visibleGroups.map(([group, items]) => <div className="supply-group" key={group}>
      {(group !== 'Still needed' || visibleGroups.length > 1) && <h3>{group}</h3>}
      <ul className="supply-items">{items.map(item => {
        const query = materialQuery(item);
        const gathered = group === 'Gathered';
        const expanded = expandedId === item.id;
        return <SupplyRow key={item.id} item={item} gathered={gathered} expanded={expanded}
          result={results.get(query)} searching={queries.has(query)}
          onToggle={() => {
            if (!expanded && gathered) request(query);
            setExpandedId(expanded ? null : item.id);
          }}
          onCheck={value => {
            focusId.current = item.id;
            if (expanded) setExpandedId(null);
            setChecked(item.id, value);
          }}
          onRetry={() => { request(query); retry(query); }} />;
      })}</ul>
    </div>)}</div>
  </section>;
}
