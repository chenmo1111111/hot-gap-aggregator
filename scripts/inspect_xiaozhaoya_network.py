"""Print Xiaozhaoya XHR/fetch request metadata for collector maintenance."""

from __future__ import annotations

import asyncio
import json

from playwright.async_api import async_playwright


async def main() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()

        def request(request):
            if request.resource_type in {"xhr", "fetch"}:
                print(json.dumps({
                    "method": request.method, "url": request.url,
                }, ensure_ascii=False))

        page.on("request", request)
        await page.goto("https://www.xiaozhaoya.com/home", wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(10_000)
        print("TITLE", await page.title())
        print("URL", page.url)
        data = await page.evaluate("""() => {
          const node = [...document.querySelectorAll('*')].find(el => el.__vue__ && Array.isArray(el.__vue__.$data?.recruitmentList));
          if (!node) return null;
          const vm = node.__vue__;
          return {total: vm.$data.total, currentPage: vm.$data.currentPage, pageSize: vm.$data.pageSize,
            first: vm.$data.recruitmentList[0], dataKeys: Object.keys(vm.$data || {}),
            methods: Object.keys(vm.$options.methods || {}),
            fetchData: String(vm.fetchData || '').slice(0, 4000),
            handleCurrentChange: String(vm.handleCurrentChange || '').slice(0, 1200)};
        }""")
        print("DATA", json.dumps(data, ensure_ascii=False))
        paging = await page.evaluate("""async () => {
          const node = [...document.querySelectorAll('*')].find(el => el.__vue__ && Array.isArray(el.__vue__.$data?.recruitmentList));
          const vm = node.__vue__;
          await vm.handleSizeChange(100);
          await new Promise(resolve => setTimeout(resolve, 2000));
          const firstPage = (vm.$data.recruitmentList || []).map(row => row.recruitmentId);
          await vm.handleCurrentChange(2);
          await new Promise(resolve => setTimeout(resolve, 2000));
          return {pageSize: vm.$data.pageSize, currentPage: vm.$data.currentPage,
            firstPage, secondPage: (vm.$data.recruitmentList || []).map(row => row.recruitmentId)};
        }""")
        print("PAGING", json.dumps(paging, ensure_ascii=False))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
