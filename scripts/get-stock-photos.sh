#!/usr/bin/env bash
# Downloads the stock photo picks (free under the Unsplash License) into frontend/dashboard/img/stock/
cd "$(dirname "$0")/.."
mkdir -p frontend/dashboard/img/stock
for id in 1714972383523-7c636d2f0e9b 1714972383570-44ddc9738355 1597333935203-9915d87fe06e 1695281535578-ce371088bb3e 1699730164892-d7c433524ff3 1754684223400-813ee3b9213c 1517457373958-b7bdd4587205 1499309221416-e18ee3411c38 1758272133593-14965a8b8f53 1761246969211-4757c50faf45 1698270723408-c4c25cd39074 1509824227185-9c5a01ceba0d 1482731910308-31e96e5d1d28 1769541706531-9182ebb963db 1623191817319-d38fa1884cf4 1665072202409-381b92d8ec2e 1635369422552-5f60fcd64340 1666090257483-32f8b567424b 1587825140708-dfaf72ae4b04 1531058020387-3be344556be6 ; do
  out="frontend/dashboard/img/stock/$id.jpg"
  if [ -s "$out" ]; then echo "have $id"; continue; fi
  if curl -sSfL "https://images.unsplash.com/photo-$id?w=1200&q=80&fm=jpg&fit=crop&crop=faces" -o "$out"; then echo "got  $id"; else echo "FAILED $id"; rm -f "$out"; fi
done
echo "Done. Open frontend/dashboard/preview-photos.html in your browser."
