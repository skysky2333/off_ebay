const galleryImage = document.querySelector("[data-gallery-image]");
const galleryLink = document.querySelector("[data-gallery-link]");
const galleryThumbs = document.querySelectorAll("[data-gallery-thumb]");
const imageViewer = document.querySelector("[data-image-viewer]");
const imageViewerImage = document.querySelector("[data-image-viewer-image]");
const imageViewerClose = document.querySelector("[data-image-viewer-close]");

for (const thumbnail of galleryThumbs) {
  thumbnail.addEventListener("click", (event) => {
    if (!galleryImage || !galleryLink) return;
    event.preventDefault();
    galleryImage.hidden = false;
    galleryLink.hidden = false;
    galleryImage.src = thumbnail.dataset.imageSrc;
    galleryImage.alt = thumbnail.dataset.imageAlt;
    galleryLink.href = thumbnail.dataset.imageSrc;
    galleryLink.dataset.imageSrc = thumbnail.dataset.imageSrc;
    galleryLink.dataset.imageAlt = thumbnail.dataset.imageAlt;
    galleryLink.setAttribute("aria-label", thumbnail.dataset.imageLabel);
    for (const other of galleryThumbs) other.setAttribute("aria-current", "false");
    thumbnail.setAttribute("aria-current", "true");
  });
}

if (galleryLink && imageViewer && imageViewerImage) {
  galleryLink.addEventListener("click", (event) => {
    if (typeof imageViewer.showModal !== "function") return;
    event.preventDefault();
    imageViewerImage.src = galleryLink.dataset.imageSrc || galleryLink.href;
    imageViewerImage.alt = galleryLink.dataset.imageAlt || galleryImage?.alt || "";
    imageViewer.showModal();
  });

  imageViewer.addEventListener("click", (event) => {
    const bounds = imageViewer.getBoundingClientRect();
    const outside =
      event.clientX < bounds.left ||
      event.clientX > bounds.right ||
      event.clientY < bounds.top ||
      event.clientY > bounds.bottom;
    if (outside) imageViewer.close();
  });
}

if (imageViewer && imageViewerClose) {
  imageViewerClose.addEventListener("click", () => imageViewer.close());
}

for (const stepper of document.querySelectorAll("[data-quantity-stepper]")) {
  const input = stepper.querySelector("input[type='number']");
  const decrement = stepper.querySelector("[data-step='down']");
  const increment = stepper.querySelector("[data-step='up']");
  const syncButtons = () => {
    const value = input.valueAsNumber;
    const minimum = Number(input.min);
    const maximum = Number(input.max);
    const invalid = input.disabled || !Number.isFinite(value);
    decrement.disabled = invalid || value <= minimum;
    increment.disabled = invalid || value >= maximum;
  };
  stepper.addEventListener("click", (event) => {
    const direction = event.target.closest("[data-step]")?.dataset.step;
    if (!direction) return;
    direction === "up" ? input.stepUp() : input.stepDown();
    syncButtons();
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  input.addEventListener("input", syncButtons);
  input.addEventListener("change", syncButtons);
  syncButtons();
}

const variantSelector = document.querySelector("[data-variant-selector]");

if (variantSelector) {
  const directPrice = document.querySelector("[data-product-price]");
  const ebayPrice = document.querySelector("[data-ebay-price]");
  const prefix = document.querySelector("[data-price-prefix]");
  const quantity = document.querySelector("[data-product-quantity]");
  const initialDirectPrice = directPrice.textContent;
  const initialEbayPrice = ebayPrice.textContent;
  const initialPrefix = prefix.textContent;
  const initialMaximum = quantity.max;
  variantSelector.addEventListener("change", () => {
    const option = variantSelector.selectedOptions[0];
    directPrice.textContent = option.dataset.directPrice || initialDirectPrice;
    ebayPrice.textContent = option.dataset.ebayPrice || initialEbayPrice;
    prefix.textContent = option.dataset.directPrice ? "" : initialPrefix;
    quantity.max = option.dataset.quantity || initialMaximum;
    if (Number(quantity.value) > Number(quantity.max)) quantity.value = quantity.max;
  });
}

const paypalCheckout = document.querySelector("[data-paypal-checkout]");

if (paypalCheckout && window.paypal) {
  const form = document.querySelector(paypalCheckout.dataset.form);
  const error = document.querySelector("[data-checkout-error]");
  const csrfToken = form.querySelector("input[name='csrfmiddlewaretoken']").value;
  const loading = document.querySelector("[data-paypal-loading]");

  const requestJson = async (url, options) => {
    const response = await fetch(url, options);
    const payload = await response.json();
    if (!response.ok) {
      const requestError = new Error(payload.error);
      requestError.customerSafe = true;
      requestError.code = payload.code;
      throw requestError;
    }
    return payload;
  };

  const buttons = window.paypal.Buttons({
    style: { layout: "vertical", color: "black", shape: "rect", label: "paypal", height: 48 },
    createOrder: async () => {
      error.hidden = true;
      if (!form.reportValidity()) {
        const validationError = new Error("Complete the highlighted shipping details.");
        validationError.customerSafe = true;
        throw validationError;
      }
      const payload = await requestJson(paypalCheckout.dataset.createUrl, {
        method: "POST",
        headers: { "X-CSRFToken": csrfToken, "X-Requested-With": "XMLHttpRequest" },
        body: new FormData(form),
      });
      if (payload.redirect_url) {
        window.location.assign(payload.redirect_url);
        return new Promise(() => {});
      }
      return payload.paypal_order_id;
    },
    onApprove: async (data, actions) => {
      error.hidden = true;
      try {
        const payload = await requestJson(paypalCheckout.dataset.captureUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": csrfToken,
            "X-Requested-With": "XMLHttpRequest",
          },
          body: JSON.stringify({ paypal_order_id: data.orderID }),
        });
        window.location.assign(payload.redirect_url);
      } catch (captureError) {
        if (captureError.code === "INSTRUMENT_DECLINED") return actions.restart();
        throw captureError;
      }
    },
    onError: (checkoutError) => {
      error.hidden = false;
      error.textContent = checkoutError.customerSafe
        ? checkoutError.message
        : "Payment could not be completed. Please try again.";
    },
  });
  buttons.render(paypalCheckout).then(
    () => { loading.hidden = true; },
    () => { loading.textContent = "PayPal could not load. Refresh the page to try again."; },
  );
} else if (paypalCheckout) {
  document.querySelector("[data-paypal-loading]").textContent =
    "PayPal could not load. Refresh the page to try again.";
}

const copyOrderLink = document.querySelector("[data-copy-order-link]");

if (copyOrderLink) {
  const status = document.querySelector("[data-copy-order-status]");
  copyOrderLink.addEventListener("click", () => {
    if (!navigator.clipboard) {
      status.textContent = "Private order link could not be copied.";
      status.hidden = false;
      return;
    }
    navigator.clipboard.writeText(window.location.href).then(
      () => {
        status.textContent = "Private order link copied.";
        status.hidden = false;
      },
      () => {
        status.textContent = "Private order link could not be copied.";
        status.hidden = false;
      },
    );
  });
}
