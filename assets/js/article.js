window.addEventListener('load', function(){
    if (location.hash === '#aggregoctt-embedded') {
        MagicMenu.style.display = document.querySelector('footer').style.display = 'none';
        document.body.style.padding = 0;
        
        var bar = document.querySelector('.alertbar');
        bar.classList.remove('auto');
        bar.style.position = 'unset';
        bar.style.display = 'block !important';
        bar.querySelector('.row').style.display = 'unset';

        var items = bar.querySelectorAll('div.col-md-6');
        for (var i=0; i<items.length; i++) {
            items[i].style.margin = '1em';
            if (i === 1) {
                items[i].style.float = 'right';
            }
            items[i].querySelector('a').href += '#aggregoctt-embedded';
        }
    }
});