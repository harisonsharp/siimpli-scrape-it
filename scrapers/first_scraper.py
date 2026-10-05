from bs4 import BeautifulSoup
from typing import List, Dict, Any

from utils.browser import captcha_gate

URL = (
    "https://www.scrapmonster.com/metal-prices/minor-metals"
)

def has_className(tag):
    return tag.has_attr('class')
def run(params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    """
    Scrapes all minor metal prices from scrapmonster.com
    """
    with captcha_gate(URL) as page:
        # "load" waits for the DOM + subresources but NOT open streaming
        # connections — safe for live-data sites that never reach networkidle.
        html = page.content()

    soup = BeautifulSoup(html, "html.parser")
    data = []
    for tag in soup.find_all(has_className):

        # if "data-test" is a tag:
        for child in tag.descendants:
            if child.name == "h2" and child['class'][0] == "h2heading":
                data.append({
                    "tag": child.name,
                    "text": child.string.strip(),
                })

        
    return data
