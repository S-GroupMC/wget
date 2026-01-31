#!/usr/bin/env node
/**
 * Puppeteer Crawler - Downloads websites with JavaScript rendering
 * Handles: pagination, "Show More" buttons, infinite scroll, SPAs
 */

const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');
const { URL } = require('url');

// Parse command line arguments
const args = process.argv.slice(2);
const config = {
    url: args[0],
    outputDir: args[1] || './output',
    maxPages: parseInt(args[2]) || 100,
    scrollPages: args.includes('--scroll'),
    clickShowMore: args.includes('--click-more'),
    waitTime: parseInt(args.find(a => a.startsWith('--wait='))?.split('=')[1]) || 2000,
    depth: parseInt(args.find(a => a.startsWith('--depth='))?.split('=')[1]) || 3
};

if (!config.url) {
    console.error('Usage: node puppeteer-crawler.js <url> [output_dir] [max_pages] [--scroll] [--click-more] [--wait=ms] [--depth=n]');
    process.exit(1);
}

const visitedUrls = new Set();
const downloadedAssets = new Set();
const baseUrl = new URL(config.url);

async function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function autoScroll(page) {
    await page.evaluate(async () => {
        await new Promise((resolve) => {
            let totalHeight = 0;
            const distance = 500;
            const maxScrolls = 50;
            let scrollCount = 0;
            
            const timer = setInterval(() => {
                const scrollHeight = document.body.scrollHeight;
                window.scrollBy(0, distance);
                totalHeight += distance;
                scrollCount++;
                
                if (totalHeight >= scrollHeight || scrollCount >= maxScrolls) {
                    clearInterval(timer);
                    resolve();
                }
            }, 200);
        });
    });
}

async function clickShowMoreButtons(page) {
    const showMoreSelectors = [
        'button:contains("Show More")',
        'button:contains("Load More")',
        'button:contains("See More")',
        'a:contains("Show More")',
        'a:contains("Load More")',
        '[class*="load-more"]',
        '[class*="show-more"]',
        '[data-action="load-more"]',
        '.pagination a[rel="next"]',
        'button[class*="next"]'
    ];
    
    for (const selector of showMoreSelectors) {
        try {
            const buttons = await page.$$(selector);
            for (const button of buttons) {
                const isVisible = await button.isIntersectingViewport();
                if (isVisible) {
                    await button.click();
                    console.log(`Clicked: ${selector}`);
                    await sleep(config.waitTime);
                }
            }
        } catch (e) {
            // Selector not found, continue
        }
    }
}

function sanitizeFilename(url) {
    const parsed = new URL(url);
    let filename = parsed.pathname;
    if (filename === '/' || filename === '') {
        filename = '/index.html';
    }
    if (!path.extname(filename)) {
        filename += '.html';
    }
    return filename.replace(/[<>:"|?*]/g, '_');
}

async function saveResource(url, content, outputDir) {
    const filename = sanitizeFilename(url);
    const filepath = path.join(outputDir, filename);
    const dir = path.dirname(filepath);
    
    if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
    }
    
    fs.writeFileSync(filepath, content);
    console.log(`Saved: ${filepath}`);
    return filepath;
}

async function extractLinks(page, currentUrl) {
    const links = await page.evaluate(() => {
        const anchors = document.querySelectorAll('a[href]');
        return Array.from(anchors).map(a => a.href);
    });
    
    return links.filter(link => {
        try {
            const url = new URL(link);
            return url.hostname === baseUrl.hostname && !visitedUrls.has(link);
        } catch {
            return false;
        }
    });
}

async function crawlPage(browser, url, depth = 0) {
    if (visitedUrls.has(url) || depth > config.depth || visitedUrls.size >= config.maxPages) {
        return;
    }
    
    visitedUrls.add(url);
    console.log(`[${visitedUrls.size}/${config.maxPages}] Crawling: ${url} (depth: ${depth})`);
    
    const page = await browser.newPage();
    
    try {
        // Set user agent
        await page.setUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
        
        // Navigate to page
        await page.goto(url, { waitUntil: 'networkidle2', timeout: 30000 });
        
        // Wait for dynamic content
        await sleep(config.waitTime);
        
        // Scroll to load lazy content
        if (config.scrollPages) {
            console.log('  Scrolling page...');
            await autoScroll(page);
            await sleep(1000);
        }
        
        // Click "Show More" buttons
        if (config.clickShowMore) {
            console.log('  Looking for Show More buttons...');
            for (let i = 0; i < 5; i++) {
                await clickShowMoreButtons(page);
            }
        }
        
        // Get rendered HTML
        const html = await page.content();
        
        // Save page
        const outputDir = path.join(config.outputDir, baseUrl.hostname);
        await saveResource(url, html, outputDir);
        
        // Extract and follow links
        const links = await extractLinks(page, url);
        console.log(`  Found ${links.length} new links`);
        
        await page.close();
        
        // Crawl linked pages
        for (const link of links.slice(0, 20)) {
            await crawlPage(browser, link, depth + 1);
        }
        
    } catch (error) {
        console.error(`  Error: ${error.message}`);
        await page.close();
    }
}

async function main() {
    console.log('='.repeat(50));
    console.log('Puppeteer Crawler');
    console.log('='.repeat(50));
    console.log(`URL: ${config.url}`);
    console.log(`Output: ${config.outputDir}`);
    console.log(`Max pages: ${config.maxPages}`);
    console.log(`Scroll: ${config.scrollPages}`);
    console.log(`Click Show More: ${config.clickShowMore}`);
    console.log(`Wait time: ${config.waitTime}ms`);
    console.log(`Depth: ${config.depth}`);
    console.log('='.repeat(50));
    
    const browser = await puppeteer.launch({
        headless: 'new',
        args: ['--no-sandbox', '--disable-setuid-sandbox']
    });
    
    try {
        await crawlPage(browser, config.url, 0);
        console.log('\n' + '='.repeat(50));
        console.log(`Completed! Downloaded ${visitedUrls.size} pages`);
        console.log('='.repeat(50));
    } finally {
        await browser.close();
    }
}

main().catch(console.error);
