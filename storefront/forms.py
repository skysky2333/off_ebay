from django import forms
from django.core.validators import RegexValidator


US_REGIONS = (
    ("", "Select state"),
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"),
    ("AR", "Arkansas"), ("CA", "California"), ("CO", "Colorado"),
    ("CT", "Connecticut"), ("DE", "Delaware"), ("DC", "District of Columbia"),
    ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"),
    ("ID", "Idaho"), ("IL", "Illinois"), ("IN", "Indiana"),
    ("IA", "Iowa"), ("KS", "Kansas"), ("KY", "Kentucky"),
    ("LA", "Louisiana"), ("ME", "Maine"), ("MD", "Maryland"),
    ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"),
    ("MS", "Mississippi"), ("MO", "Missouri"), ("MT", "Montana"),
    ("NE", "Nebraska"), ("NV", "Nevada"), ("NH", "New Hampshire"),
    ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"),
    ("OK", "Oklahoma"), ("OR", "Oregon"), ("PA", "Pennsylvania"),
    ("RI", "Rhode Island"), ("SC", "South Carolina"), ("SD", "South Dakota"),
    ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"),
    ("VT", "Vermont"), ("VA", "Virginia"), ("WA", "Washington"),
    ("WV", "West Virginia"), ("WI", "Wisconsin"), ("WY", "Wyoming"),
    ("AS", "American Samoa"), ("GU", "Guam"), ("MP", "Northern Mariana Islands"),
    ("PR", "Puerto Rico"), ("VI", "U.S. Virgin Islands"),
)


class CheckoutForm(forms.Form):
    customer_email = forms.EmailField(
        widget=forms.EmailInput(attrs={"autocomplete": "email"})
    )
    customer_phone = forms.CharField(
        max_length=40,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "tel"}),
    )
    customer_name = forms.CharField(
        max_length=300,
        widget=forms.TextInput(attrs={"autocomplete": "name"}),
    )
    shipping_line_1 = forms.CharField(
        max_length=300,
        widget=forms.TextInput(attrs={"autocomplete": "address-line1"}),
    )
    shipping_line_2 = forms.CharField(
        max_length=300,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "address-line2"}),
    )
    shipping_city = forms.CharField(
        max_length=120,
        widget=forms.TextInput(attrs={"autocomplete": "address-level2"}),
    )
    shipping_region = forms.ChoiceField(
        choices=US_REGIONS,
        widget=forms.Select(attrs={"autocomplete": "address-level1"}),
    )
    shipping_postal_code = forms.CharField(
        max_length=10,
        validators=[RegexValidator(r"^\d{5}(?:-\d{4})?$", "Enter a valid U.S. ZIP code.")],
        widget=forms.TextInput(
            attrs={
                "autocomplete": "postal-code",
                "inputmode": "numeric",
                "pattern": r"\d{5}(?:-\d{4})?",
            }
        ),
    )
    shipping_country_code = forms.CharField(
        initial="US", widget=forms.HiddenInput()
    )

    def clean_shipping_country_code(self):
        country = self.cleaned_data["shipping_country_code"].upper()
        if country != "US":
            raise forms.ValidationError("Only United States addresses are supported.")
        return country
