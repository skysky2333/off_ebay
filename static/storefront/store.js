const menuToggle = document.querySelector("[data-menu-toggle]");
const siteNav = document.querySelector("[data-site-nav]");

if (menuToggle && siteNav) {
  menuToggle.addEventListener("click", () => {
    const open = menuToggle.getAttribute("aria-expanded") !== "true";
    menuToggle.setAttribute("aria-expanded", String(open));
    menuToggle.querySelector(".sr-only").textContent = open ? "Close menu" : "Open menu";
    siteNav.dataset.open = String(open);
  });
}

const galleryImage = document.querySelector("[data-gallery-image]");
const galleryButtons = document.querySelectorAll("[data-gallery-thumb]");

for (const button of galleryButtons) {
  button.addEventListener("click", () => {
    galleryImage.src = button.dataset.imageSrc;
    galleryImage.alt = button.dataset.imageAlt;
    for (const other of galleryButtons) other.setAttribute("aria-current", "false");
    button.setAttribute("aria-current", "true");
  });
}

for (const stepper of document.querySelectorAll("[data-quantity-stepper]")) {
  const input = stepper.querySelector("input[type='number']");
  stepper.addEventListener("click", (event) => {
    const direction = event.target.closest("[data-step]")?.dataset.step;
    if (!direction) return;
    direction === "up" ? input.stepUp() : input.stepDown();
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

const variantSelector = document.querySelector("[data-variant-selector]");

if (variantSelector) {
  const price = document.querySelector("[data-product-price]");
  const prefix = document.querySelector("[data-price-prefix]");
  const quantity = document.querySelector("[data-product-quantity]");
  variantSelector.addEventListener("change", () => {
    const option = variantSelector.selectedOptions[0];
    if (!option.dataset.price) return;
    price.textContent = option.dataset.price;
    prefix.textContent = "";
    quantity.max = option.dataset.quantity;
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
      throw requestError;
    }
    return payload;
  };

  const buttons = window.paypal.Buttons({
    style: { layout: "vertical", color: "black", shape: "rect", label: "paypal", height: 48 },
    createOrder: async () => {
      if (!form.reportValidity()) throw new Error("Shipping details are incomplete.");
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
    onApprove: async (data) => {
      const payload = await requestJson(paypalCheckout.dataset.captureUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
        body: JSON.stringify({ paypal_order_id: data.orderID }),
      });
      window.location.assign(payload.redirect_url);
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
