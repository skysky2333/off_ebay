import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from xml.etree import ElementTree

import httpx
import nh3
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import URLValidator
from django.utils.dateparse import parse_datetime

from .account_state import account_closure_notification_id
from .models import EbayAccountClosure


XML_NAMESPACE = "urn:ebay:apis:eBLBaseComponents"
NS = {"e": XML_NAMESPACE}
SUPPORTED_LISTING_TYPES = {"FixedPriceItem", "StoresFixedPrice"}
ACTIVE_LISTING_STATUS = "Active"
EBAY_MARKETPLACE_ID = "EBAY_US"
MARKETING_PAGE_SIZE = 200
validate_https_url = URLValidator(schemes=("https",))
DESCRIPTION_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "caption",
    "code",
    "dd",
    "del",
    "dl",
    "dt",
    "em",
    "figcaption",
    "figure",
    "h3",
    "h4",
    "hr",
    "i",
    "ins",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "small",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
DESCRIPTION_ATTRIBUTES = {"a": {"href"}}
ElementTree.register_namespace("", XML_NAMESPACE)


class EbayError(Exception):
    pass


class EbayResponseError(EbayError):
    pass


class EbayInventoryConflict(EbayResponseError):
    pass


@dataclass(frozen=True)
class EbayImage:
    url: str
    variation_name: str = ""
    variation_value: str = ""


@dataclass(frozen=True)
class EbayVariation:
    source_key: str
    sku: str
    title: str
    specifics: dict
    price: Decimal
    quantity: int
    purchasable: bool


@dataclass(frozen=True)
class EbayListing:
    item_id: str
    title: str
    description: str
    price: Decimal
    currency: str
    condition: str
    category_id: str
    category_name: str
    item_specifics: dict
    shipping: dict
    listing_url: str
    listing_type: str
    listing_status: str
    quantity: int
    started_at: object
    ends_at: object
    images: tuple
    variations: tuple
    volume_discounts: tuple = ()


@dataclass(frozen=True)
class EbayUserIdentity:
    username: str
    eias_token: str


def _element(name, text=None):
    node = ElementTree.Element(f"{{{XML_NAMESPACE}}}{name}")
    if text is not None:
        node.text = str(text)
    return node


def _child(parent, name, text=None):
    node = ElementTree.SubElement(parent, f"{{{XML_NAMESPACE}}}{name}")
    if text is not None:
        node.text = str(text)
    return node


def _text(parent, path, default=""):
    node = parent.find(path, NS)
    return default if node is None or node.text is None else node.text


def _required_text(parent, path):
    value = _text(parent, path)
    if not value:
        raise EbayResponseError(f"eBay response is missing {path}")
    return value


def _available_quantity(parent):
    total = int(_required_text(parent, "e:Quantity"))
    sold = int(_text(parent, "e:SellingStatus/e:QuantitySold", "0"))
    return max(total - sold, 0)


def _specifics(parent, path="e:ItemSpecifics/e:NameValueList"):
    result = {}
    for entry in parent.findall(path, NS):
        name = _required_text(entry, "e:Name")
        result[name] = [value.text or "" for value in entry.findall("e:Value", NS)]
    return result


def _money(parent, path):
    node = parent.find(path, NS)
    if node is None or node.text is None:
        raise EbayResponseError(f"eBay response is missing {path}")
    currency = node.attrib.get("currencyID")
    if not currency:
        raise EbayResponseError(f"eBay response is missing {path} currencyID")
    try:
        value = Decimal(node.text)
    except InvalidOperation as error:
        raise EbayResponseError(f"eBay response has invalid {path} amount") from error
    if not value.is_finite() or value < 0:
        raise EbayResponseError(f"eBay response has invalid {path} amount")
    return value, currency


def _date(parent, path):
    value = parse_datetime(_required_text(parent, path))
    if value is None:
        raise EbayResponseError(f"eBay response has invalid {path}")
    return value


def _shipping(item):
    dispatch_time_max = _text(item, "e:DispatchTimeMax")
    if dispatch_time_max and (
        not dispatch_time_max.isascii() or not dispatch_time_max.isdigit()
    ):
        raise EbayResponseError("eBay response has invalid DispatchTimeMax")
    details = item.find("e:ShippingDetails", NS)
    if details is None:
        return {"dispatch_time_max": dispatch_time_max} if dispatch_time_max else {}
    services = []
    for option in details.findall("e:ShippingServiceOptions", NS):
        cost = option.find("e:ShippingServiceCost", NS)
        additional = option.find("e:ShippingServiceAdditionalCost", NS)
        services.append(
            {
                "service": _text(option, "e:ShippingService"),
                "cost": cost.text if cost is not None else "",
                "additional_cost": additional.text if additional is not None else "",
                "free_shipping": _text(option, "e:FreeShipping") == "true",
            }
        )
    return {
        "type": _text(details, "e:ShippingType"),
        "services": services,
        "ship_to_locations": [
            node.text or "" for node in item.findall("e:ShipToLocations", NS)
        ],
        "excluded_locations": [
            node.text or "" for node in details.findall("e:ExcludeShipToLocation", NS)
        ],
        "dispatch_time_max": dispatch_time_max,
        "location": _text(item, "e:Location"),
        "country": _text(item, "e:Country"),
        "postal_code": _text(item, "e:PostalCode"),
    }


def _promotion_volume_discount(promotion):
    if not isinstance(promotion, dict):
        raise EbayResponseError("eBay volume discount response is invalid")
    if promotion.get("promotionStatus") != "RUNNING":
        raise EbayResponseError("eBay volume discount is not running")
    if promotion.get("promotionType") != "VOLUME_DISCOUNT":
        raise EbayResponseError("eBay promotion is not a volume discount")

    criterion = promotion.get("inventoryCriterion")
    listing_ids = criterion.get("listingIds") if isinstance(criterion, dict) else None
    if (
        not isinstance(listing_ids, list)
        or not listing_ids
        or any(not isinstance(item_id, str) or not item_id for item_id in listing_ids)
        or len(set(listing_ids)) != len(listing_ids)
    ):
        raise EbayResponseError("eBay volume discount has invalid listing IDs")
    single_item_only = promotion.get("applyDiscountToSingleItemOnly")
    if not isinstance(single_item_only, bool):
        raise EbayResponseError("eBay volume discount has invalid item scope")
    if not single_item_only and len(listing_ids) > 1:
        raise EbayResponseError(
            "eBay volume discount combines multiple listings and cannot be priced"
        )

    rules = promotion.get("discountRules")
    if not isinstance(rules, list) or not 2 <= len(rules) <= 4:
        raise EbayResponseError("eBay volume discount has invalid rules")
    ordered_rules = []
    rule_orders = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise EbayResponseError("eBay volume discount has invalid rules")
        rule_order = rule.get("ruleOrder")
        if (
            isinstance(rule_order, bool)
            or not isinstance(rule_order, int)
            or rule_order < 1
            or rule_order in rule_orders
        ):
            raise EbayResponseError("eBay volume discount has invalid rule order")
        rule_orders.add(rule_order)
        ordered_rules.append((rule_order, rule))
    if rule_orders != set(range(1, len(rules) + 1)):
        raise EbayResponseError("eBay volume discount has invalid rule order")

    discounts = []
    previous_percent = Decimal("-1")
    for expected_quantity, (_, rule) in enumerate(sorted(ordered_rules), start=1):
        specification = rule.get("discountSpecification")
        benefit = rule.get("discountBenefit")
        minimum = (
            specification.get("minQuantity")
            if isinstance(specification, dict)
            else None
        )
        percent_text = (
            benefit.get("percentageOffOrder")
            if isinstance(benefit, dict)
            else None
        )
        if (
            isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or minimum != expected_quantity
        ):
            raise EbayResponseError("eBay response has invalid volume discount quantity")
        if not isinstance(percent_text, str):
            raise EbayResponseError(
                "eBay response has invalid volume discount percentage"
            )
        try:
            percent = Decimal(percent_text)
        except InvalidOperation as error:
            raise EbayResponseError(
                "eBay response has invalid volume discount percentage"
            ) from error
        if not percent.is_finite() or percent < 0 or percent >= 100:
            raise EbayResponseError(
                "eBay response has invalid volume discount percentage"
            )
        if minimum == 1:
            if percent != 0:
                raise EbayResponseError(
                    "eBay response has invalid volume discount baseline"
                )
            previous_percent = percent
            continue
        if percent <= previous_percent:
            raise EbayResponseError(
                "eBay response has invalid volume discount percentage"
            )
        previous_percent = percent
        discounts.append(
            {
                "min_quantity": minimum,
                "percent_off": format(percent.normalize(), "f"),
            }
        )
    return tuple(listing_ids), tuple(
        sorted(discounts, key=lambda tier: tier["min_quantity"])
    )


def _https_url(url, kind):
    try:
        validate_https_url(url)
    except ValidationError as error:
        raise EbayResponseError(
            f"eBay response has invalid HTTPS {kind} URL"
        ) from error
    return url


def _image(url, variation_name="", variation_value=""):
    return EbayImage(_https_url(url, "image"), variation_name, variation_value)


def _images(item):
    images = [
        _image(node.text)
        for node in item.findall("e:PictureDetails/e:PictureURL", NS)
        if node.text
    ]
    seen = {image.url for image in images}
    for pictures in item.findall("e:Variations/e:Pictures", NS):
        name = _required_text(pictures, "e:VariationSpecificName")
        for picture_set in pictures.findall("e:VariationSpecificPictureSet", NS):
            value = _required_text(picture_set, "e:VariationSpecificValue")
            for node in picture_set.findall("e:PictureURL", NS):
                if node.text and node.text not in seen:
                    images.append(_image(node.text, name, value))
                    seen.add(node.text)
    gallery = _text(item, "e:PictureDetails/e:GalleryURL")
    if not images and gallery:
        images.append(_image(gallery))
    return tuple(images)


def _variation_source_key(sku, specifics):
    if sku:
        return sku
    signature = json.dumps(specifics, sort_keys=True, separators=(",", ":"))
    return f"missing-{hashlib.sha256(signature.encode()).hexdigest()[:24]}"


def _variation_title(specifics):
    groups = list(specifics.items())
    if len(groups) == 1:
        return " / ".join(groups[0][1])[:255]
    return " / ".join(
        f"{name}: {', '.join(values)}" for name, values in groups
    )[:255]


def _variation_is_targetable(sku, specifics):
    return bool(sku) or (
        bool(specifics)
        and all(values and all(values) for values in specifics.values())
    )


def _variations(item, currency):
    variations = []
    source_keys = set()
    for variation in item.findall("e:Variations/e:Variation", NS):
        sku = _text(variation, "e:SKU")
        specifics = _specifics(
            variation, "e:VariationSpecifics/e:NameValueList"
        )
        source_key = _variation_source_key(sku, specifics)
        if source_key in source_keys:
            raise EbayResponseError("eBay response has duplicate variation identity")
        source_keys.add(source_key)
        price, variation_currency = _money(variation, "e:StartPrice")
        if variation_currency != currency:
            raise EbayResponseError(
                f"eBay variation currency {variation_currency} does not match "
                f"listing currency {currency}"
            )
        variations.append(
            EbayVariation(
                source_key=source_key,
                sku=sku,
                title=_variation_title(specifics),
                specifics=specifics,
                price=price,
                quantity=_available_quantity(variation),
                purchasable=_variation_is_targetable(sku, specifics),
            )
        )
    return tuple(variations)


def parse_listing(response):
    item = response.find("e:Item", NS)
    if item is None:
        raise EbayResponseError("GetItem response is missing Item")
    title = _required_text(item, "e:Title")
    price, currency = _money(item, "e:StartPrice")
    variations = _variations(item, currency)
    if variations:
        available_prices = [
            variation.price
            for variation in variations
            if variation.purchasable and variation.quantity > 0
        ]
        if available_prices:
            price = min(available_prices)
        quantity = sum(variation.quantity for variation in variations)
    else:
        quantity = _available_quantity(item)
    return EbayListing(
        item_id=_required_text(item, "e:ItemID"),
        title=title,
        description=nh3.clean(
            _text(item, "e:Description"),
            tags=DESCRIPTION_TAGS,
            clean_content_tags={"script", "style"},
            attributes=DESCRIPTION_ATTRIBUTES,
            url_schemes={"https", "mailto", "tel"},
            url_relative="deny",
        ),
        price=price,
        currency=currency,
        condition=_text(item, "e:ConditionDisplayName"),
        category_id=_text(item, "e:PrimaryCategory/e:CategoryID"),
        category_name=_text(item, "e:PrimaryCategory/e:CategoryName"),
        item_specifics=_specifics(item),
        shipping=_shipping(item),
        listing_url=_https_url(
            _required_text(item, "e:ListingDetails/e:ViewItemURL"), "listing"
        ),
        listing_type=_required_text(item, "e:ListingType"),
        listing_status=_required_text(item, "e:SellingStatus/e:ListingStatus"),
        quantity=quantity,
        started_at=_date(item, "e:ListingDetails/e:StartTime"),
        ends_at=_date(item, "e:ListingDetails/e:EndTime"),
        images=_images(item),
        variations=variations,
    )


class EbayTradingClient:
    def __init__(self, transport=None):
        self._ensure_account_open()
        required = {
            "EBAY_CLIENT_ID": settings.EBAY_CLIENT_ID,
            "EBAY_CLIENT_SECRET": settings.EBAY_CLIENT_SECRET,
            "EBAY_REFRESH_TOKEN": settings.EBAY_REFRESH_TOKEN,
            "EBAY_COMPATIBILITY_LEVEL": settings.EBAY_COMPATIBILITY_LEVEL,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ImproperlyConfigured(f"Missing eBay settings: {', '.join(missing)}")
        self.http = httpx.Client(timeout=30, transport=transport)
        self.access_token = ""
        self._volume_discounts = None
        self._volume_discount_quotes = set()

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        self.close()
        return False

    def close(self):
        self.http.close()

    @staticmethod
    def _ensure_account_open():
        if account_closure_notification_id() or EbayAccountClosure.objects.exists():
            raise ImproperlyConfigured("The eBay seller account is closed.")

    def refresh_access_token(self):
        self._ensure_account_open()
        response = self.http.post(
            settings.EBAY_TOKEN_ENDPOINT,
            auth=(settings.EBAY_CLIENT_ID, settings.EBAY_CLIENT_SECRET),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": settings.EBAY_REFRESH_TOKEN,
            },
        )
        response.raise_for_status()
        self.access_token = response.json()["access_token"]
        return self.access_token

    def _call(self, name, request):
        self._ensure_account_open()
        if not self.access_token:
            self.refresh_access_token()
        response = self.http.post(
            settings.EBAY_TRADING_ENDPOINT,
            headers={
                "Content-Type": "text/xml",
                "X-EBAY-API-CALL-NAME": name,
                "X-EBAY-API-COMPATIBILITY-LEVEL": settings.EBAY_COMPATIBILITY_LEVEL,
                "X-EBAY-API-SITEID": "0",
                "X-EBAY-API-IAF-TOKEN": self.access_token,
            },
            content=ElementTree.tostring(
                request, encoding="utf-8", xml_declaration=True
            ),
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        ack = _required_text(root, "e:Ack")
        if ack not in {"Success", "Warning"}:
            errors = [
                ": ".join(
                    filter(
                        None,
                        [
                            _text(error, "e:ErrorCode"),
                            _text(error, "e:LongMessage")
                            or _text(error, "e:ShortMessage"),
                        ],
                    )
                )
                for error in root.findall("e:Errors", NS)
            ]
            raise EbayResponseError("; ".join(errors) or f"eBay {name} failed")
        return root

    def _marketing_get(self, resource, params=None):
        self._ensure_account_open()
        if not self.access_token:
            self.refresh_access_token()
        response = self.http.get(
            f"{settings.EBAY_MARKETING_ENDPOINT.rstrip('/')}/{resource}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.access_token}",
                "Content-Language": "en-US",
            },
            params=params,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            try:
                failure = response.json()
            except ValueError:
                failure = None
            messages = []
            if isinstance(failure, dict) and isinstance(failure.get("errors"), list):
                for entry in failure["errors"]:
                    if not isinstance(entry, dict):
                        continue
                    message = entry.get("message")
                    error_id = entry.get("errorId")
                    if isinstance(message, str):
                        messages.append(
                            f"{error_id}: {message}"
                            if isinstance(error_id, (int, str))
                            and not isinstance(error_id, bool)
                            else message
                        )
            detail = "; ".join(filter(None, messages))
            raise EbayResponseError(
                detail
                or f"eBay Marketing request failed with HTTP {response.status_code}"
            ) from error
        try:
            payload = response.json()
        except ValueError as error:
            raise EbayResponseError(
                "eBay Marketing response is not valid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise EbayResponseError("eBay Marketing response is invalid")
        return payload

    def _volume_discount_snapshot(self):
        summaries = []
        promotion_ids = set()
        offset = 0
        while True:
            page = self._marketing_get(
                "promotion",
                {
                    "marketplace_id": EBAY_MARKETPLACE_ID,
                    "promotion_status": "RUNNING",
                    "promotion_type": "VOLUME_DISCOUNT",
                    "limit": MARKETING_PAGE_SIZE,
                    "offset": offset,
                },
            )
            total = page.get("total")
            promotions = page.get("promotions", [])
            if (
                isinstance(total, bool)
                or not isinstance(total, int)
                or total < 0
                or not isinstance(promotions, list)
            ):
                raise EbayResponseError("eBay promotion page is invalid")
            if not promotions and offset < total:
                raise EbayResponseError("eBay promotion pagination ended early")
            for summary in promotions:
                if not isinstance(summary, dict):
                    raise EbayResponseError("eBay promotion summary is invalid")
                promotion_id = summary.get("promotionId")
                if (
                    not isinstance(promotion_id, str)
                    or not promotion_id
                    or promotion_id in promotion_ids
                    or summary.get("promotionStatus") != "RUNNING"
                    or summary.get("promotionType") != "VOLUME_DISCOUNT"
                ):
                    raise EbayResponseError("eBay promotion summary is invalid")
                promotion_ids.add(promotion_id)
                summaries.append(promotion_id)
            offset += len(promotions)
            if offset >= total:
                break

        by_item = {}
        changed_during_snapshot = False
        for promotion_id in summaries:
            promotion = self._marketing_get(
                f"item_promotion/{quote(promotion_id, safe='')}"
            )
            if promotion.get("promotionId") != promotion_id:
                raise EbayResponseError("eBay promotion detail ID does not match")
            if promotion.get("promotionType") != "VOLUME_DISCOUNT":
                raise EbayResponseError("eBay promotion is not a volume discount")
            status = promotion.get("promotionStatus")
            if not isinstance(status, str):
                raise EbayResponseError("eBay volume discount status is invalid")
            if status != "RUNNING":
                changed_during_snapshot = True
                continue
            listing_ids, tiers = _promotion_volume_discount(promotion)
            for item_id in listing_ids:
                if item_id in by_item:
                    raise EbayResponseError(
                        f"eBay listing {item_id} has overlapping volume discounts"
                    )
                by_item[item_id] = tiers
        return by_item, changed_during_snapshot

    def volume_discounts(self, refresh=False):
        if refresh:
            self._volume_discounts = None
        if self._volume_discounts is not None:
            return self._volume_discounts

        for attempt in range(2):
            by_item, changed_during_snapshot = self._volume_discount_snapshot()
            if not changed_during_snapshot or attempt == 1:
                self._volume_discounts = by_item
                return self._volume_discounts
        raise AssertionError("Unreachable volume discount snapshot state")

    def get_user(self):
        response = self._call("GetUser", _element("GetUserRequest"))
        return EbayUserIdentity(
            username=_required_text(response, "e:User/e:UserID"),
            eias_token=_required_text(response, "e:User/e:EIASToken"),
        )

    def out_of_stock_control_enabled(self):
        request = _element("GetUserPreferencesRequest")
        _child(request, "ShowOutOfStockControlPreference", "true")
        response = self._call("GetUserPreferences", request)
        return _required_text(response, "e:OutOfStockControlPreference") == "true"

    def verify_seller(self):
        identity = self.get_user()
        if identity.username.casefold() != settings.EBAY_SELLER_USERNAME.casefold():
            raise EbayResponseError(
                f"eBay token belongs to {identity.username}, not {settings.EBAY_SELLER_USERNAME}"
            )
        if not self.out_of_stock_control_enabled():
            raise EbayResponseError("eBay out-of-stock control must be enabled")
        return identity

    def active_item_ids(self):
        page = 1
        item_ids = []
        while True:
            request = _element("GetMyeBaySellingRequest")
            _child(request, "DetailLevel", "ReturnAll")
            active = _child(request, "ActiveList")
            _child(active, "Include", "true")
            pagination = _child(active, "Pagination")
            _child(pagination, "EntriesPerPage", "200")
            _child(pagination, "PageNumber", page)
            response = self._call("GetMyeBaySelling", request)
            for item in response.findall("e:ActiveList/e:ItemArray/e:Item", NS):
                item_ids.append(_required_text(item, "e:ItemID"))
            total_pages = int(
                _required_text(
                    response, "e:ActiveList/e:PaginationResult/e:TotalNumberOfPages"
                )
            )
            if page >= total_pages:
                return item_ids
            page += 1

    def get_item_without_volume_discounts(self, item_id):
        request = _element("GetItemRequest")
        _child(request, "DetailLevel", "ReturnAll")
        _child(request, "IncludeItemSpecifics", "true")
        _child(request, "ItemID", item_id)
        return parse_listing(self._call("GetItem", request))

    def get_item(self, item_id):
        listing = self.get_item_without_volume_discounts(item_id)
        refresh = listing.item_id in self._volume_discount_quotes
        discounts = self.volume_discounts(refresh=refresh)
        self._volume_discount_quotes.add(listing.item_id)
        return replace(
            listing,
            volume_discounts=discounts.get(listing.item_id, ()),
        )

    def revise_variation_inventory(
        self,
        item_id,
        quantity,
        message_id,
        source_key,
        specifics,
        price,
        currency,
    ):
        if (
            not _variation_is_targetable("", specifics)
            or _variation_source_key("", specifics) != source_key
        ):
            raise ValueError("SKU-less variation identity is invalid")
        request = _element("ReviseFixedPriceItemRequest")
        _child(request, "MessageID", message_id)
        item = _child(request, "Item")
        _child(item, "ItemID", item_id)
        variations = _child(item, "Variations")
        variation = _child(variations, "Variation")
        start_price = _child(variation, "StartPrice", price)
        start_price.set("currencyID", currency)
        _child(variation, "Quantity", quantity)
        variation_specifics = _child(variation, "VariationSpecifics")
        for name, values in specifics.items():
            entry = _child(variation_specifics, "NameValueList")
            _child(entry, "Name", name)
            for value in values:
                _child(entry, "Value", value)
        self._call("ReviseFixedPriceItem", request)
        listing = self.get_item_without_volume_discounts(item_id)
        matches = [
            variation
            for variation in listing.variations
            if variation.source_key == source_key
        ]
        if len(matches) != 1:
            raise EbayResponseError(
                "GetItem did not return the revised SKU-less variation"
            )
        verified = matches[0].quantity
        if verified != quantity:
            raise EbayResponseError(
                f"eBay inventory verification expected {quantity}, returned {verified}"
            )
        return verified

    def revise_inventory_status(self, item_id, quantity, message_id, sku=""):
        request = _element("ReviseInventoryStatusRequest")
        _child(request, "MessageID", message_id)
        status = _child(request, "InventoryStatus")
        _child(status, "ItemID", item_id)
        if sku:
            _child(status, "SKU", sku)
        _child(status, "Quantity", quantity)
        self._call("ReviseInventoryStatus", request)
        listing = self.get_item_without_volume_discounts(item_id)
        if sku:
            matches = [
                variation for variation in listing.variations if variation.sku == sku
            ]
            if len(matches) != 1:
                raise EbayResponseError(f"GetItem did not return variation SKU {sku}")
            verified = matches[0].quantity
        else:
            verified = listing.quantity
        if verified != quantity:
            raise EbayResponseError(
                f"eBay inventory verification expected {quantity}, returned {verified}"
            )
        return verified
