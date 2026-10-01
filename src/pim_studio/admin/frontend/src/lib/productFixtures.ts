import type { Market, ProductSummary } from './api'

/**
 * Fixture for the Products page, until `/api/products` exists.
 *
 * Shaped like a real season's catalogue: the sub-categories and
 * their conditional attributes come from the AW26 product-cards file, the
 * per-market coverage skew (PL best, HU worst) matches the Akinon EE table,
 * and the defect mix reflects what was actually measured — wrong-language
 * Slovak descriptions, names that differ across sizes, off-vocabulary values.
 *
 * Deliberately includes products that are fine, products blocked on us, and
 * products blocked on the partner team, so every branch of the UI has
 * something to render.
 */

type Spec = [
  mc: string,
  name: string,
  season: string,
  main: string,
  sub: string,
  gender: string,
  skus: number,
  live: string, // one char per market in PL CZ SK HU order: y / n
  blocked: string, // reason, applied to every non-live market
  missing: string[],
  defects: string[],
]

const SPECS: Spec[] = [
  ['L3558.132', 'Classic Fit Long Sleeved N.10.10 Polo Shirt', '26SS', 'Textile', 'Polo', 'MAN', 11, 'yynn', 'No price', [], ['Description in wrong language']],
  ['L3539.001', 'Original N.10.10 Fine Piqué Polo Shirt', '26SS', 'Textile', 'Polo', 'MAN', 11, 'yyyy', '', [], []],
  ['L3539.166', 'Original N.10.10 Fine Piqué Polo Shirt', '26SS', 'Textile', 'Polo', 'MAN', 10, 'yyyy', '', [], ['Name differs across sizes']],
  ['PH9619.031', 'Slim Fit Stretch Piqué Polo Shirt', '26SS', 'Textile', 'Polo', 'MAN', 9, 'yyny', 'Out of stock', ['Collar type'], []],
  ['TH3477.476', 'Aero-Dry Sport Polo', '26AW', 'Textile', 'Polo', 'MAN', 8, 'nnnn', 'Not opened in Omnitron', ['Product name', 'Description', 'Category', 'Composition'], []],
  ['TH2568.144', 'Crew Neck Cotton T-shirt', '26SS', 'Textile', 'T-shirt', 'MAN', 11, 'yyyn', 'No price', ['Description'], []],
  ['TH0769.CCA', 'Regular Fit V-Neck Pima Cotton T-shirt', 'OUTLET', 'Textile', 'T-shirt', 'MAN', 9, 'yynn', 'Out of stock', [], ['Description in wrong language']],
  ['DH7117.05H', 'Loose Fit Printed Cotton T-shirt', '26SS', 'Textile', 'T-shirt', 'WOMAN', 7, 'yyyy', '', [], []],
  ['SH6262.57I', 'Hooded Fleece Sweatshirt', '26AW', 'Textile', 'Sweatshirt', 'MAN', 8, 'ynnn', 'No price', ['Sleeve type', 'Description'], []],
  ['SH8407.031', 'Crew Neck Cotton Blend Sweatshirt', '26AW', 'Textile', 'Sweatshirt', 'UNISEX', 8, 'yyyy', '', ['Sleeve type'], []],
  ['AH7093.27R', 'Wool Blend Crew Neck Sweater', '26AW', 'Textile', 'Sweater', 'MAN', 7, 'yynn', 'No price', ['Sleeve type', 'Collar type'], []],
  ['AF7987.166', 'Ribbed Wool Turtleneck Sweater', '26AW', 'Textile', 'Sweater', 'WOMAN', 6, 'nnnn', 'Not opened in Omnitron', ['Product name', 'Description', 'Category', 'Fit'], []],
  ['BH2749.S5T', 'Commuter Waterproof Jacket', '26AW', 'Textile', 'Jacket', 'MAN', 6, 'ynnn', 'No price', ['Description'], []],
  ['BH3313.CB8', 'Quilted Water-Repellent Jacket', '26AW', 'Textile', 'Jacket', 'MAN', 7, 'yyyy', '', [], ['Fit outside allowed list']],
  ['HH3309.031', 'Lightweight Padded Parka', '25AW', 'Textile', 'Jacket', 'WOMAN', 6, 'yyyn', 'Out of stock', [], []],
  ['CH5371.TG5', 'Cotton Twill Chino Trousers', '26SS', 'Textile', 'Trousers', 'MAN', 9, 'yyyy', '', [], []],
  ['GF5591.001', 'Cotton Fleece Track Trousers', 'OUTLET', 'Textile', 'Track trousers', 'WOMAN', 7, 'yyny', 'Out of stock', ['Description'], []],
  ['MH2777.08P', 'Stretch Cotton Shorts', 'OUTLET', 'Textile', 'Short', 'MAN', 8, 'yyyy', '', [], []],
  ['60KFA0791.83J', 'Metro Evo Leather Sneakers', '26SS', 'Footwear', 'Sneakers', 'MAN', 10, 'yyyn', 'No price', [], []],
  ['719RFA8779.GS2', 'Baseline Pro Textile Sneakers', '26SS', 'Footwear', 'Sneakers', 'WOMAN', 9, 'yyyy', '', [], []],
  ['722KMA5991.NB0', 'Court Frame Leather Sneakers', '26AW', 'Footwear', 'Sneakers', 'MAN', 9, 'nnnn', 'Not opened in Omnitron', ['Product name', 'Description', 'Category', 'Composition'], []],
  ['5H1291.BCK', '3-Pack Stretch Cotton Trunks', '26SS', 'Underwear', 'Trunk', 'MAN', 5, 'yyyy', '', [], []],
  ['5H1299.031', '3-Pack Printed Stretch Trunks', '26SS', 'Underwear', 'Trunk', 'MAN', 5, 'yynn', 'No price', ['Description'], []],
  ['NF8574.166', 'Zip Leather Wallet', '26AW', 'Leather Goods', 'Wallet', 'MAN', 1, 'yyyy', '', [], []],
  ['NF8823.021', 'Small Grained Leather Crossbody Bag', '26AW', 'Leather Goods', 'Bag', 'WOMAN', 1, 'yynn', 'No price', ['Composition'], []],
  ['RK5707.031', 'Embroidered Cotton Cap', 'OUTLET', 'Leather Goods', 'Cap', 'UNISEX', 3, 'yyyy', '', [], []],
  ['3196.130', 'Northwind Heritage Watch 42mm', '25AW', 'Leather Goods', 'Accessories', 'MAN', 1, 'ynnn', 'Not in catalogue', ['Description', 'Composition'], []],
  ['PF3380.WDT', 'Ribbed Cotton Polo Dress', '26SS', 'Textile', 'Dress', 'WOMAN', 7, 'yyyn', 'Out of stock', ['Sleeve type'], ['Name differs across sizes']],
]

const MARKETS: Market[] = ['PL', 'CZ', 'SK', 'HU']

export const DEMO_PRODUCTS: ProductSummary[] = SPECS.map(
  ([mc, name, season, main, sub, gender, skus, live, blocked, missing, defects]) => {
    const liveMap: Partial<Record<Market, boolean>> = {}
    const blockedMap: Partial<Record<Market, string>> = {}
    MARKETS.forEach((m, i) => {
      const isLive = live[i] === 'y'
      liveMap[m] = isLive
      if (!isLive && blocked) blockedMap[m] = blocked
    })
    return {
      modelColor: mc,
      name,
      imageUrl: null,
      season,
      mainCategory: main,
      subCategory: sub,
      gender,
      skuCount: skus,
      live: liveMap,
      blockedBy: blockedMap,
      missing,
      defects,
    }
  },
)
