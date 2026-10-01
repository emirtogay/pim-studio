import type { ProductDetail, VocabularyOption } from './api'

/**
 * Fixture for the Product Detail page — L3558.132, the demo product from the
 * design handoff (`docs/design/Product Detail - Attribute Matrix.html`).
 *
 * This exists so the page is buildable and reviewable before the backend
 * does. It mirrors real data shapes measured from actual exports — the
 * wrong-language Slovak description, the missing Hungarian price, the
 * one-size stock hole — so the UI is exercised against the defects it was
 * designed around, not a happy path. Delete when `/api/products/{mc}` lands.
 */

const SLEEVE: VocabularyOption[] = [
  { value: 'LONG SLEEVES', status: 'approved', usageCount: 102 },
  { value: 'SHORT-SLEEVES', status: 'approved', usageCount: 1240 },
  { value: 'SLEEVELESS', status: 'approved', usageCount: 88 },
]

const FIT: VocabularyOption[] = [
  { value: 'CLASSIC FIT', status: 'approved', usageCount: 812 },
  { value: 'SLIM FIT', status: 'approved', usageCount: 644 },
  { value: 'REGULAR FIT', status: 'approved', usageCount: 590 },
  { value: 'LOOSE FIT', status: 'approved', usageCount: 121 },
  { value: 'OVERSIZE FIT', status: 'approved', usageCount: 74 },
  { value: 'FLARE FIT', status: 'approved', usageCount: 12 },
  { value: 'RELAXED FIT', status: 'candidate', usageCount: 3 },
]

const COLOUR_FILTER: VocabularyOption[] = [
  { value: 'BLACK', status: 'approved', usageCount: 690 },
  { value: 'BLUE', status: 'approved', usageCount: 656 },
  { value: 'WHITE', status: 'approved', usageCount: 512 },
  { value: 'GREEN', status: 'approved', usageCount: 348 },
  { value: 'BEIGE', status: 'approved', usageCount: 201 },
  { value: 'RED', status: 'approved', usageCount: 176 },
  { value: 'BROWN', status: 'approved', usageCount: 98 },
  { value: 'GREY', status: 'approved', usageCount: 233 },
  { value: 'PINK', status: 'approved', usageCount: 87 },
  { value: 'KHAKI GREEN', status: 'candidate', usageCount: 1 },
]

const COLLAR: VocabularyOption[] = [
  { value: 'Crew neck', status: 'approved', usageCount: 402 },
  { value: 'Polo collar', status: 'approved', usageCount: 388 },
  { value: 'V-neck', status: 'approved', usageCount: 120 },
  { value: 'Hooded neck', status: 'approved', usageCount: 96 },
  { value: 'Buttoned crew neck', status: 'approved', usageCount: 31 },
  { value: 'Hooded zippered neck', status: 'approved', usageCount: 27 },
]

export const DEMO_PRODUCT: ProductDetail = {
  modelColor: 'L3558.132',
  name: 'Classic Fit Long Sleeved N.10.10 Polo Shirt',
  baseCode: 'L3558',
  colorCode: '132',
  colorName: 'GREEN',
  season: '26SS',
  gender: 'MAN',
  category: 'Textile',
  subCategory: 'Polo',
  sizeCount: 11,
  lastChangedAt: '6 Aug 2026',
  lastChangedVia: 'masterdata import',
  markets: [
    { market: 'PL', live: true, reason: '' },
    { market: 'CZ', live: true, reason: '' },
    { market: 'SK', live: false, reason: 'Out of stock — 0 units across 11 sizes. Priced, imaged, categorised.' },
    { market: 'HU', live: false, reason: 'No price, and name is still a proposal. Stock and images fine.' },
  ],
  groups: [
    {
      title: 'Per-market content',
      rows: [
        {
          key: 'product_name',
          label: 'product name',
          owner: 'us',
          perMarket: {
            PL: { state: 'live', value: 'Polo Classic Fit N.10.10', source: 'omnitron export', changedAt: '2 Jul' },
            CZ: { state: 'live', value: 'Polo Classic Fit N.10.10' },
            SK: { state: 'exported', value: 'Polo Classic Fit N.10.10' },
            HU: {
              state: 'proposed',
              value: 'N.10.10 klasszikus galléros póló',
              proposedValue: 'N.10.10 klasszikus galléros póló',
              proposedAt: '11 Aug',
              source: '26AW_masterdata_v3.xlsx',
            },
          },
        },
        {
          key: 'product_description',
          label: 'product description',
          owner: 'us',
          longText: true,
          perMarket: {
            PL: { state: 'live', value: 'Ikoniczne polo w miękkiej piqué…' },
            CZ: { state: 'live', value: 'Ikonické polo z jemného piké…' },
            SK: {
              state: 'live',
              value: 'The iconic polo, reinvented…',
              suspect: 'EN?',
              suspectReason:
                'Language check: 82% of tokens match English, not Slovak. Likely the PL/EN source was pasted into the SK field.',
              changedAt: 'live since 2 Jul',
              changedBy: 'Ada K. · 30 Jun 2026 · confirmed live 2 Jul',
              source: '26AW_masterdata_v3.xlsx',
              sourceDetail: 'sheet EN UK Database · row 1,204',
              proposedValue:
                'Ikonické polo, prepracované do mäkkého piké bavlny s rebrovaným golierom a gombíkmi z perlete.',
              proposedAt: '11 Aug',
            },
            HU: { state: 'approved', value: 'Az ikonikus póló, újragondolva…' },
          },
        },
        {
          key: 'long_description',
          label: 'long description',
          owner: 'us',
          longText: true,
          perMarket: {
            PL: { state: 'live', value: '<ul> 6 bullets' },
            CZ: { state: 'exported', value: '<ul> 6 bullets' },
            SK: { state: 'missing' },
            HU: { state: 'missing' },
          },
        },
        {
          key: 'price',
          label: 'price',
          owner: 'us',
          perMarket: {
            PL: { state: 'live', value: '479,00 PLN', code: true },
            CZ: { state: 'live', value: '2 790 CZK', code: true },
            SK: { state: 'live', value: '109,00 EUR', code: true },
            HU: { state: 'missing' },
          },
        },
        {
          key: 'internal_comment',
          label: 'internal comment',
          owner: 'us',
          perMarket: {
            PL: { state: 'live', value: 'wave 2' },
            CZ: { state: 'missing' },
            SK: { state: 'rejected', value: 'do not publish' },
            HU: { state: 'missing' },
          },
        },
      ],
    },
    {
      title: 'Classification — shared across markets',
      rows: [
        { key: 'main_category', label: 'main category', owner: 'partner', shared: { state: 'live', value: 'Textile' } },
        {
          key: 'sub_category',
          label: 'sub-category',
          owner: 'partner',
          shared: {
            state: 'stale',
            value: 'Polo',
            suspect: 'DIVERGED',
            suspectReason: 'Upstream now says "Polo Shirt"; our value is "Polo". Diverged since 4 Aug.',
          },
        },
        { key: 'gender', label: 'gender', owner: 'partner', shared: { state: 'live', value: 'MAN', code: true } },
        { key: 'season', label: 'season', owner: 'system', shared: { state: 'live', value: '26SS', code: true } },
        { key: 'fit', label: 'fit', owner: 'partner', shared: { state: 'live', value: 'CLASSIC FIT', code: true }, vocabulary: FIT },
        { key: 'sleeve_type', label: 'sleeve type', owner: 'partner', shared: { state: 'live', value: 'LONG SLEEVES', code: true }, vocabulary: SLEEVE },
        { key: 'collar_type', label: 'collar type', owner: 'partner', shared: { state: 'missing' }, vocabulary: COLLAR },
        { key: 'pattern', label: 'pattern', owner: 'partner', shared: { state: 'na' } },
        { key: 'composition', label: 'composition', owner: 'partner', shared: { state: 'exported', value: 'Cotton 100%' } },
        { key: 'colour_detail', label: 'colour detail', owner: 'us', shared: { state: 'approved', value: 'Solid' } },
        { key: 'colour_filter', label: 'colour filter', owner: 'us', shared: { state: 'live', value: 'GREEN', code: true }, vocabulary: COLOUR_FILTER },
      ],
    },
    {
      title: 'Read-only — derived',
      rows: [
        { key: 'base_code', label: 'base code', owner: 'system', shared: { state: 'live', value: 'L3558', code: true } },
        { key: 'colour_code', label: 'colour code', owner: 'system', shared: { state: 'live', value: '132', code: true } },
        {
          key: 'stock',
          label: 'stock',
          owner: 'system',
          perMarket: {
            PL: { state: 'live', value: '312 units', code: true },
            CZ: { state: 'live', value: '96 units', code: true },
            SK: { state: 'live', value: '0 units', code: true, suspect: 'ZERO' },
            HU: { state: 'live', value: '54 units', code: true },
          },
        },
        {
          key: 'discount_pct',
          label: 'discount %',
          owner: 'system',
          perMarket: {
            PL: { state: 'na' },
            CZ: { state: 'live', value: '−20%', code: true },
            SK: { state: 'na' },
            HU: { state: 'na' },
          },
        },
        {
          key: 'is_discounted',
          label: 'is discounted',
          owner: 'system',
          perMarket: {
            PL: { state: 'live', value: 'false', code: true },
            CZ: { state: 'live', value: 'true', code: true },
            SK: { state: 'live', value: 'false', code: true },
            HU: { state: 'live', value: 'false', code: true },
          },
        },
      ],
    },
  ],
  sizes: [
    { label: 'T1 / XS', stock: { PL: 0, CZ: 0, SK: 0, HU: 0 }, price: { PL: '479,00', CZ: '2 790', SK: '109,00' } },
    { label: 'T2 / S', stock: { PL: 38, CZ: 12, SK: 0, HU: 6 }, price: { PL: '479,00', CZ: '2 790', SK: '109,00' } },
    { label: 'T3 / M', stock: { PL: 96, CZ: 30, SK: 0, HU: 16 }, price: { PL: '479,00', CZ: '2 790', SK: '109,00' } },
    { label: 'T4 / L', stock: { PL: 74, CZ: 23, SK: 0, HU: 13 }, price: { PL: '479,00', CZ: '2 232', SK: '109,00' } },
    { label: 'T5 / XL', stock: { PL: 52, CZ: 16, SK: 0, HU: 9 }, price: { PL: '479,00', CZ: '2 232', SK: '109,00' } },
    { label: 'T6 / XXL', stock: { PL: 31, CZ: 10, SK: 0, HU: 5 }, price: { PL: '479,00', CZ: '2 790', SK: '109,00' } },
    { label: 'T7', stock: { PL: 12, CZ: 4, SK: 0, HU: 2 }, price: { PL: '479,00', CZ: '2 790', SK: '109,00' } },
    { label: 'T8', stock: { PL: 6, CZ: 2, SK: 0, HU: 1 }, price: { PL: '479,00', CZ: '2 790', SK: '109,00' } },
    { label: 'T9', stock: { PL: 3, CZ: 1, SK: 0, HU: 1 }, price: { PL: '479,00', CZ: '2 790', SK: '109,00' } },
    { label: 'T10', stock: { PL: 0, CZ: 0, SK: 0, HU: 0 }, price: { PL: '479,00', CZ: '2 790', SK: '109,00' } },
    { label: 'T11', stock: { PL: 0, CZ: 0, SK: 0, HU: 0 }, price: {} },
  ],
  blockers: [
    {
      owner: 'us',
      label: 'Ours — 3 actions',
      items: [
        { scope: 'HU', text: 'No price. Blocks Hungary going live.' },
        { scope: 'SK', text: 'Description present but reads as English — review or retranslate.' },
        { scope: 'HU', text: 'Product name still a proposal from 26AW_masterdata.xlsx.' },
      ],
    },
    {
      owner: 'partner',
      label: 'Partner team, Turkey — chase only',
      items: [
        { scope: 'ALL', text: 'Collar type missing. Requested 4 Aug, no response.' },
        { scope: 'ALL', text: 'Sub-category diverged upstream: Polo → Polo Shirt.' },
      ],
    },
    {
      owner: 'nobody',
      label: 'Nobody — waiting on a system',
      items: [{ scope: 'SK', text: 'Stock is 0 across all sizes. Resolves on next stock feed.' }],
    },
  ],
  images: [
    { position: 1, url: null, state: 'live' },
    { position: 2, url: null, state: 'live' },
    { position: 3, url: null, state: 'live' },
    { position: 4, url: null, state: 'proposed' },
    { position: 5, url: null, state: 'proposed' },
  ],
}
